(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  // v5.31.2 — single source of truth for the Session KPI color map.
  // Used by Main, Val, and Gene panels so the label → color mapping
  // can never drift. Backend emits PRE / OR / OPEN / POWER / AFTER /
  // CLOSED based on real ET time (dashboard_server.py /api/state).
  // DEFENSIVE / CHOP are kept for backward-compat.
  function __tgSessionColor(mode) {
    switch (mode) {
      case "DEFENSIVE": return "var(--down)";
      case "CHOP":
      case "POWER":
      case "OR":      return "var(--warn)";
      case "PRE":
      case "AFTER":   return "var(--text-dim)";
      case "CLOSED":
      case "\u2014":  return "var(--text-muted)";
      case "OPEN":    return "var(--up)";
      default:        return "var(--up)";
    }
  }

  // v4.1.8-dash — Robinhood view was removed in v3.5.0 along with the
  // RH portfolio payload. The toggle + storage machinery + slice()
  // indirection lingered as dead code. Now simplified: only the paper
  // portfolio is ever rendered, so the slice is computed inline where
  // needed and lastSnapshot stays for any future re-render on demand.
  let lastSnapshot = null;

  function paperSlice(s) {
    return {
      portfolio: s.portfolio || {},
      positions: s.positions || [],
      trades: s.trades_today || [],
      view: "paper",
    };
  }

  function fmtUsd(v, digits = 2) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    const neg = v < 0;
    const s = Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
    return (neg ? "−$" : "$") + s;
  }
  function fmtPx(v) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return "$" + Number(v).toFixed(2);
  }
  // v8.3.18 -- time-in-position formatter for the OPEN POSITIONS
  // "Held" column. Computes (now - entry_ts_utc) and renders as one
  // of "Nm" / "Nh Nm" / "Nd Nh". Bot-internal stamps use UTC ISO
  // strings; the math is timezone-agnostic so we stay correct in
  // both EDT and EST without DST handling.
  function fmtHeld(entryIso) {
    if (!entryIso) return "—";
    try {
      const d = new Date(entryIso);
      const t = d.getTime();
      if (!isFinite(t)) return "—";
      const seconds = Math.floor((Date.now() - t) / 1000);
      if (seconds < 0) return "—";
      const minutes = Math.floor(seconds / 60);
      if (minutes < 60) return minutes + "m";
      const hours = Math.floor(minutes / 60);
      const mins = minutes % 60;
      if (hours < 24) return hours + "h " + mins + "m";
      const days = Math.floor(hours / 24);
      const rem = hours % 24;
      return days + "d " + rem + "h";
    } catch (e) { return "—"; }
  }
  window.fmtHeld = fmtHeld;  // exposed for IIFE-2 (Val/Gene tabs)

  // ET minutes-since-midnight (0-1439) for EOD time bar; Intl.DateTimeFormat handles DST.
  function __tgNowEtMinutes() {
    try {
      var _parts = new Intl.DateTimeFormat("en-US", {
        timeZone: "America/New_York",
        hour: "numeric", minute: "numeric", hour12: false,
      }).formatToParts(new Date());
      var _h = 0, _m = 0;
      for (var _i = 0; _i < _parts.length; _i++) {
        if (_parts[_i].type === "hour")   _h = parseInt(_parts[_i].value) || 0;
        if (_parts[_i].type === "minute") _m = parseInt(_parts[_i].value) || 0;
      }
      return _h * 60 + _m;
    } catch (_e) {
      return ((new Date().getUTCHours() - 4 + 24) % 24) * 60 + new Date().getUTCMinutes();
    }
  }
  window.__tgNowEtMinutes = __tgNowEtMinutes;

  function fmtPct(v, digits) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    const abs = Math.abs(v);
    const d = digits ?? (abs < 0.1 ? 3 : 2);
    return (v >= 0 ? "+" : "−") + abs.toFixed(d) + "%";
  }

  // v7.89.0 -- timestamps shown to the operator are converted from
  // UTC (the storage format) into US/Eastern (ET) regardless of the
  // browser's local timezone, so two operators on different coasts
  // see the same clock. Functions kept their utcIsoToLocal* names
  // for backwards compatibility with all call sites; chart x-axis
  // math still uses ET buckets (utcIsoToEtMin elsewhere) so display
  // and bucketing now agree.
  // v7.82.0 first introduced browser-local rendering; v7.89.0
  // pinned the display zone to ET to match the bot's market clock.
  function utcIsoToLocalHHMM(iso) {
    if (!iso) return "";
    try {
      const d = new Date(iso);
      if (isNaN(d.getTime())) return String(iso);
      return d.toLocaleTimeString("en-US", {
        hour: "2-digit", minute: "2-digit",
        hour12: false,
        timeZone: "America/New_York",
        timeZoneName: "short",
      });
    } catch (e) {
      return String(iso);
    }
  }
  function utcIsoToLocalFull(iso) {
    if (!iso) return "";
    try {
      const d = new Date(iso);
      if (isNaN(d.getTime())) return String(iso);
      return d.toLocaleString("en-US", {
        year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit", second: "2-digit",
        hour12: false,
        timeZone: "America/New_York",
        timeZoneName: "short",
      });
    } catch (e) {
      return String(iso);
    }
  }
  function cls(el, add, rm = []) {
    rm.forEach((c) => el.classList.remove(c));
    if (Array.isArray(add)) add.forEach((c) => el.classList.add(c));
    else if (add) el.classList.add(add);
  }
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
  }

  // v5.17.0 — GATE tri-state helper retired. The Gate KPI tile was
  // dropped from Main + Val/Gene KPI rows (gate state is now surfaced
  // through the new Weather Check banner on Main). Health-pill helper
  // remains — still used for per-executor error counts.

  // v4.11.0 — per-executor health pill renderer + dropdown.
  // Two separate IIFEs touch this: Main /api/state via __tgOnState, and
  // the Val/Gene poll loop in the second IIFE via window.__tgApplyHealthPill
  // (mirrors the v4.10.2 applyGateTriState bridge pattern).
  const __HEALTH_CLASSES = ["h-green", "h-warn", "h-red"];
  function __tgFormatErrTs(ts) {
    if (!ts) return "";
    try {
      const d = new Date(ts);
      if (isNaN(d.getTime())) return String(ts);
      // v7.89.0 -- render in US/Eastern instead of browser-local so
      // error timestamps line up with the rest of the dashboard.
      return d.toLocaleTimeString("en-US", {
        hour: "2-digit", minute: "2-digit", second: "2-digit",
        hour12: false,
        timeZone: "America/New_York",
      });
    } catch (e) { return String(ts); }
  }
  function applyHealthPill(executor, snap) {
    const pill = document.getElementById("tg-health-pill");
    const cnt  = document.getElementById("tg-health-count");
    const list = document.getElementById("tg-health-list");
    const title = document.getElementById("tg-health-pop-title");
    if (!pill || !cnt || !list) return;
    // Only paint if this snapshot belongs to the active tab. We read the
    // active tab off body data attribute set by selectTab().
    const active = document.body.getAttribute("data-tg-active-tab") || "main";
    if (executor !== active) return;
    const count = (snap && typeof snap.count === "number") ? snap.count : 0;
    const sev = (snap && snap.severity) || "green";
    const cls = sev === "red" ? "h-red" : sev === "warning" ? "h-warn" : "h-green";
    __HEALTH_CLASSES.forEach(c => pill.classList.remove(c));
    pill.classList.add(cls);
    cnt.textContent = String(count);
    pill.setAttribute("aria-label", `Errors today: ${count}`);
    pill.setAttribute("title", `Errors today: ${count}`);
    if (title) title.textContent = `Errors today (${executor}) · ${count}`;
    const entries = (snap && Array.isArray(snap.entries)) ? snap.entries : [];
    if (!entries.length) {
      list.innerHTML = `<div class="tg-health-empty">No errors today.</div>`;
    } else {
      list.innerHTML = entries.map(e => {
        const sevTxt = String(e.severity || "error");
        const code = String(e.code || "");
        const ts = __tgFormatErrTs(e.ts);
        const summ = String(e.summary || "");
        return `<div class="tg-health-row">
          <div class="tg-health-row-top">
            <span class="tg-health-sev ${escapeHtml(sevTxt)}">${escapeHtml(sevTxt)}</span>
            <span class="tg-health-code">${escapeHtml(code)}</span>
            <span class="tg-health-ts">${escapeHtml(ts)}</span>
          </div>
          <div class="tg-health-summary">${escapeHtml(summ)}</div>
        </div>`;
      }).join("");
    }
  }
  window.__tgApplyHealthPill = applyHealthPill;

  // v7.108.0 (lifecycle-tab fix) -- bridge the ET-zoned timestamp
  // helpers across the IIFE-1 / IIFE-2 boundary. Lifecycle tab code
  // (IIFE-2 at lines ~4727, ~4890) and the v10 activity feed
  // (IIFE-2 at line ~4053) call these by bare name and get a
  // ReferenceError ("Can't find variable: utcIsoToLocalFull"). Bug
  // existed since v7.89.0 refactored these into IIFE-1 -- surfaced
  // when the operator clicked Lifecycle. Same pattern as
  // __tgApplyHealthPill above.
  window.utcIsoToLocalHHMM = utcIsoToLocalHHMM;
  window.utcIsoToLocalFull = utcIsoToLocalFull;

  // Pop dropdown wiring — runs once per page load.
  function __tgWireHealthPop() {
    const pill = document.getElementById("tg-health-pill");
    const pop  = document.getElementById("tg-health-pop");
    const closeBtn = document.getElementById("tg-health-close");
    if (!pill || !pop) return;
    function position() {
      // Anchor below the pill, right-aligned to viewport.
      const r = pill.getBoundingClientRect();
      pop.style.top = `${Math.round(r.bottom + 6)}px`;
      pop.style.right = `16px`;
    }
    function open() {
      position();
      pop.style.display = "flex";
      pill.setAttribute("aria-expanded", "true");
    }
    function close() {
      pop.style.display = "none";
      pill.setAttribute("aria-expanded", "false");
    }
    pill.addEventListener("click", (e) => {
      e.stopPropagation();
      if (pop.style.display === "none" || !pop.style.display) open(); else close();
    });
    if (closeBtn) closeBtn.addEventListener("click", (e) => { e.stopPropagation(); close(); });
    document.addEventListener("click", (e) => {
      if (pop.style.display === "none") return;
      if (pop.contains(e.target) || pill.contains(e.target)) return;
      close();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && pop.style.display !== "none") close();
    });
    window.addEventListener("resize", () => {
      if (pop.style.display !== "none") position();
    });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", __tgWireHealthPop);
  } else {
    __tgWireHealthPop();
  }
  // Mark Main as the initial active tab so Main's /api/state errors paint.
  if (!document.body.getAttribute("data-tg-active-tab")) {
    document.body.setAttribute("data-tg-active-tab", "main");
  }

  // ─────── rendering ───────

  function renderKPIs(s, sl) {
    const p = sl.portfolio || {};
    $("k-equity").textContent = fmtUsd(p.equity);
    // v9.1.32 -- tooltip clarifies that trade sizing uses the risk-book
    // equity ($100k configured capital), not the mark-to-market portfolio
    // equity shown here. The gap grows as paper returns accumulate.
    const equityKpi = $("k-equity") && $("k-equity").closest(".kpi");
    if (equityKpi) {
      const rb = ((s.v10 || {}).risk_books || {}).main || {};
      const rbEq = rb.equity;
      if (rbEq != null && Math.abs(p.equity - rbEq) > 0.5) {
        equityKpi.title = "Mark-to-market equity (paper cash + positions). "
          + "Trade sizing uses the risk-book equity: " + fmtUsd(rbEq, 0)
          + " (configured starting capital). Gap = accumulated paper returns.";
      }
    }
    const vs = p.vs_start;
    const eqSub = $("k-equity-sub");
    eqSub.innerHTML = `Start ${fmtUsd(p.start, 0)} · <span class="${vs >= 0 ? 'delta-up' : 'delta-down'}">${fmtUsd(vs)}</span>`;

    const pnl = p.day_pnl;
    const pnlEl = $("k-pnl");
    pnlEl.textContent = fmtUsd(pnl);
    const pnlHasVal = Number.isFinite(pnl);
    pnlEl.className = "kpi-value " + (!pnlHasVal ? "" : (pnl >= 0 ? "delta-up" : "delta-down"));
    const pct = (p.start && pnlHasVal) ? (pnl / p.start) * 100 : null;
    const tradesLen = (sl.trades || []).length;
    const pctCls = Number.isFinite(pct) ? (pct >= 0 ? 'delta-up' : 'delta-down') : '';
    // v7.43.0 -- broker (Alpaca) day P&L shown as a chip alongside the
    // paper P&L.
    //
    // v7.51.0 -- the Main panel is paper-only; Alpaca holds positions
    // for Val + Gene (the per-executor Alpaca paper accounts). The
    // closed half of broker_day_pnl just duplicates the paper Day
    // P&L (close_breakout writes m.trade_log on every paper close),
    // and the open half is Alpaca's unrealized for Val+Gene leaking
    // into Main's KPI tile. Both sides are wrong for Main. Hide it.
    // Val/Gene tabs don't render this chip (their whole KPI tile is
    // already broker-shape via /api/executor/<name>).
    var brokerPnl = null;
    var brokerHtml = "";
    if (brokerPnl != null) {
      var brokerCls = brokerPnl >= 0 ? "delta-up" : "delta-down";
      brokerHtml = ' · <span class="kpi-broker ' + brokerCls
        + '" title="Broker (Alpaca) day P&L: closed (trade_log) + open (open_pnl)">'
        + 'broker ' + fmtUsd(brokerPnl);
      // Delta chip when paper and broker diverge by > $1
      if (pnlHasVal && Math.abs(brokerPnl - pnl) > 1) {
        var delta = brokerPnl - pnl;
        var deltaCls = Math.abs(delta) > 50 ? "kpi-delta-warn" : "kpi-delta-mild";
        brokerHtml += ' <span class="kpi-delta-chip ' + deltaCls
          + '" title="Broker vs paper divergence">'
          + (delta >= 0 ? "+" : "")
          + fmtUsd(delta) + '</span>';
      }
      brokerHtml += '</span>';
    }
    // v7.43.0 -- broker block gets its own line below so the delta
    // chip never wraps + clips against the KPI tile's max-height.
    $("k-pnl-sub").innerHTML =
      `${tradesLen} trade${tradesLen===1?"":"s"} · <span class="${pctCls}">${fmtPct(pct)}</span>`
      + ' <span style="color:var(--text-dim);font-size:10px" title="Day P&L source: paper state (trade_genius.py trade log). Val/Gene tabs show Alpaca live data.">paper</span>'
      + (brokerHtml ? '<div class="kpi-broker-line">' + brokerHtml.replace(/^ · /, '') + '</div>' : '');

    const positions = sl.positions || [];
    $("k-open").textContent = String(positions.length);
    if (positions.length === 0) {
      $("k-open-sub").textContent = "No positions";
    } else if (positions.length === 1) {
      const pos = positions[0];
      $("k-open-sub").innerHTML = `${escapeHtml(pos.ticker)} <span class="${pos.side === 'SHORT' ? 'side-short' : 'side-long'}">${pos.side}</span> · ${pos.shares} sh`;
    } else {
      const longs = positions.filter(x => x.side === "LONG").length;
      const shorts = positions.length - longs;
      $("k-open-sub").textContent = `${longs} long · ${shorts} short`;
    }

    // v5.17.0 — GATE + REGIME KPI tiles dropped. GATE was redundant
    // with the SESSION tile (CLOSED / RTH OPEN); REGIME (RSI direction)
    // was decommissioned with the move to QQQ AVWAP_0930 + 9-EMA
    // permits in v5.15. The shared applyGateTriState helper is still
    // exported via window.__tgApplyGateTriState for the Val/Gene
    // executor tabs which continue to render GATE on their own panels.
    const reg = s.regime || {};
    // ── Session KPI: time-of-day / risk state (OPEN / CHOP / POWER / DEFENSIVE / CLOSED)
    //    this is MarketMode in the bot — a session window, not a directional view
    const sEl = $("k-session");
    // v5.31.2 — backend now emits real session labels (PRE / OR / OPEN /
    // POWER / AFTER / CLOSED) computed from ET time.
    const mode = reg.mode || "—";
    sEl.textContent = mode;
    sEl.style.color = __tgSessionColor(mode);
    $("k-session-sub").textContent = reg.mode_reason || "—";
  }

  function renderPositions(s, sl) {
    const positions = sl.positions || [];
    // EOD reversal positions from EodReversalEngine (separate from paper_state ORB). Keyed by ticker.
    const _eodMain = s.eod_positions || {};
    const _eodMainTickers = Object.keys(_eodMain);
    const _mainPosN = positions.length + _eodMainTickers.length;
    $("pos-count").textContent = `· ${_mainPosN}`;
    const _mainBadge = document.getElementById("tg-badge-main");
    if (_mainBadge) _mainBadge.textContent = _mainPosN > 0 ? `${_mainPosN}` : "";
    const body = $("pos-body");
    // v7.89.0 -- port-strip / port-strip-empty footer blocks were
    // retired from the Open Positions card. Equity now lives in the
    // KPI row above the table (see index.html v7.89.0 reorder); the
    // Notional column (v7.87.0) inside the table covers per-position
    // invested / short-liability dollars.
    const card = body && body.parentElement;

    if (positions.length === 0 && _eodMainTickers.length === 0) {
      body.innerHTML = `<div class="empty">No open positions.</div>`;
      body.style.display = "";
      if (card) card.classList.add("is-empty");
      return;
    } else {
      body.style.display = "";
      if (card) card.classList.remove("is-empty");
      const rows = positions.map((p) => {
        const sideCls = p.side === "SHORT" ? "side-short" : "side-long";
        const markCls = p.side === "SHORT" ? "mark-short" : "mark-long";
        const pnlCls = (p.unrealized ?? 0) >= 0 ? "delta-up" : "delta-down";
        const eff = (typeof p.effective_stop === "number")
                      ? p.effective_stop : p.stop;
        // v7.2.8 \u2014 honor backend trail_pill gating. The backend
        // (_compute_trail_pill_state in dashboard_server.py) already
        // decides whether the trail has actually tightened the stop
        // tighter than the original 1R entry stop; if it returns
        // null we hide the badge here. Falling back to the legacy
        // chandelier_stage>=1 / trail_active rule rendered the pill
        // on Stage-1 BE-arm and on fresh entries where the stop was
        // still equal to the original hard stop. (v7.2.7 fix landed
        // server-side only; this is the matching client-side fix.)
        const trailBadge = (p.trail_pill && p.trail_pill.status)
          ? ` <span class="trail-badge" title="Trail stop is armed \u2014 the effective stop now follows price, not the original hard stop">TRAIL</span>`
          : "";
        // v5.13.10 — SB (Alarm A1 Loss distance) column removed
        // per operator request. Phase badge stays: A = fresh entry,
        // B = first runner / partial taken, C = mature ratcheting trail.
        const phase = (p.phase === "B" || p.phase === "C") ? p.phase : "A";
        const phaseTitle = (phase === "A")
          ? "Phase A \u2014 fresh entry, hard stop only"
          : (phase === "B")
            ? "Phase B \u2014 1R partial taken, trail arming toward breakeven"
            : "Phase C \u2014 mature runner, ratcheting trail stop";
        // Self-describing labels: OPEN / 1R\u2197 / TRAIL instead of A/B/C.
        const phaseLabel = phase === "A" ? "OPEN" : phase === "B" ? "1R\u2197" : "TRAIL";
        const phaseBadge = `<span class="eot-phase-badge eot-phase-${phase}" title="${escapeHtml(phaseTitle)}">${phaseLabel}</span>`;
        const dotTitle = (p.side === "SHORT") ? "Open short position" : "Open long position";
        // v6.0.3: % column added for parity with Val/Gene executor tabs.
        // cost basis = entry * shares; unrealized / cost_basis gives the
        // same percent the broker payload exposes as unrealized_pnl_pct.
        let pctTxt = "\u2014";
        const _entryNum = Number(p.entry);
        const _shNum = Number(p.shares);
        const _unrNum = Number(p.unrealized);
        if (Number.isFinite(_entryNum) && _entryNum > 0
            && Number.isFinite(_shNum) && _shNum > 0
            && Number.isFinite(_unrNum)) {
          pctTxt = fmtPct((_unrNum / (_entryNum * _shNum)) * 100);
        }
        // v7.42.0 -- progress bar geometry: single axis from stop to target
        // with entry / 1R / current-mark needle. RR=2.5 (v10 keystone).
        // v9.1.5 -- axis is anchored on the IMMUTABLE admission stop
        // (p.entry_stop) so the 1R / target ticks don't drift once the
        // chandelier trail moves the current stop past entry into
        // profit territory. p.effective_stop is overlaid as a separate
        // trail marker so the operator can see where the live stop has
        // moved. Pre-v9.1.5 this used p.stop (the post-trail mutated
        // value) and the formula inverted the axis whenever
        // (entry - stop) flipped sign.
        const _markNum = Number(p.mark);
        var _axisStopNum = Number(p.entry_stop);
        if (!Number.isFinite(_axisStopNum) || _axisStopNum <= 0) {
          _axisStopNum = Number(p.stop);  // legacy fallback
        }
        const _effStopNum = (typeof p.effective_stop === "number")
                              ? p.effective_stop : Number(p.stop);
        var progressRow = "";
        if (Number.isFinite(_axisStopNum) && _axisStopNum > 0
            && Number.isFinite(_entryNum) && _entryNum > 0
            && Number.isFinite(_markNum) && _markNum > 0
            && Math.abs(_entryNum - _axisStopNum) > 1e-4) {
          var isLong = p.side !== "SHORT";
          var stopPx = _axisStopNum;          // immutable axis anchor
          var entryPx = _entryNum;
          var markPx = _markNum;
          var targetPx = isLong
            ? entryPx + 2.5 * (entryPx - stopPx)
            : entryPx - 2.5 * (stopPx - entryPx);
          var span = targetPx - stopPx; // signed; negative for short
          var _toPct = function (px) {
            if (Math.abs(span) < 1e-9) return 50;
            return Math.max(0, Math.min(100, (px - stopPx) / span * 100));
          };
          var entryAt = _toPct(entryPx);
          var oneRPx = isLong
            ? entryPx + (entryPx - stopPx)
            : entryPx - (stopPx - entryPx);
          var oneRAt = _toPct(oneRPx);
          var markAt = _toPct(markPx);
          var r = isLong
            ? (markPx - entryPx) / (entryPx - stopPx)
            : (entryPx - markPx) / (stopPx - entryPx);
          var rTxt = (r >= 0 ? "+" : "") + r.toFixed(2) + "R";
          // v9.1.5 -- effective-stop indicator. When the trail has
          // tightened past the admission stop (toward entry or beyond),
          // draw a second tick at its current axis position so the
          // operator can see locked-in profit at a glance.
          var trailTick = "";
          if (Number.isFinite(_effStopNum) && _effStopNum > 0
              && Math.abs(_effStopNum - _axisStopNum) > 1e-4) {
            var trailAt = _toPct(_effStopNum);
            trailTick = '<span class="pos-progress-tick trail" '
              + 'style="left:' + trailAt.toFixed(2) + '%" '
              + 'data-label="trail" '
              + 'title="effective stop (trail): ' + fmtPx(_effStopNum) + '"></span>';
          }
          progressRow =
            '<tr class="pos-progress-row" data-pos-ticker="' + escapeHtml(p.ticker) + '">' +
              '<td colspan="11" class="pos-progress-cell">' +
                '<div class="pos-progress">' +
                  '<div class="pos-progress-track">' +
                    '<div class="pos-progress-zone red"     style="left:0%; width:' + entryAt.toFixed(2) + '%"></div>' +
                    '<div class="pos-progress-zone neutral" style="left:' + entryAt.toFixed(2) + '%; width:' + (oneRAt - entryAt).toFixed(2) + '%"></div>' +
                    '<div class="pos-progress-zone green"   style="left:' + oneRAt.toFixed(2) + '%; width:' + (100 - oneRAt).toFixed(2) + '%"></div>' +
                    '<span class="pos-progress-tick" style="left:' + entryAt.toFixed(2) + '%" data-label="entry"></span>' +
                    '<span class="pos-progress-tick" style="left:' + oneRAt.toFixed(2) + '%" data-label="1R"></span>' +
                    '<span class="pos-progress-tick end" style="left:100%" data-label="target"></span>' +
                    trailTick +
                    '<span class="pos-progress-needle ' + (r >= 0 ? 'up' : 'down') + '" style="left:' + markAt.toFixed(2) + '%">' +
                      '<span class="needle-label">' + escapeHtml(rTxt) + '</span>' +
                    '</span>' +
                  '</div>' +
                  '<div class="pos-progress-meta">' +
                    '<span class="pp-meta-left">stop ' + fmtPx(_effStopNum) + '</span>' +
                    '<span class="pp-meta-center">1R ' + fmtPx(oneRPx) + '</span>' +
                    '<span class="pp-meta-right">target ' + fmtPx(targetPx) + '</span>' +
                  '</div>' +
                '</div>' +
              '</td>' +
            '</tr>';
        }
        // v7.56.0 -- per-trade risk dollars. |entry - effective_stop|
        // * shares is exactly the number summed into the Concurrent
        // Risk gauge. Surfacing it per-row makes the gauge math
        // traceable to specific tickers.
        var _riskTxt = "—";
        var _riskShareCount = Number(p.shares);
        var _riskEntry = Number(p.entry);
        var _riskStop = Number(eff);
        if (Number.isFinite(_riskShareCount) && _riskShareCount > 0
            && Number.isFinite(_riskEntry) && _riskEntry > 0
            && Number.isFinite(_riskStop) && _riskStop > 0) {
          var _rps = Math.abs(_riskEntry - _riskStop);
          if (_rps > 0) _riskTxt = fmtUsd(_rps * _riskShareCount);
        }
        // v7.87.0 -- notional value at cost (shares * entry). For longs
        // this is the dollar amount invested; for shorts it's the
        // dollar liability outstanding. Sums of these per direction
        // feed the v7.86.0 total-exposure cap. Surfacing it per-row
        // makes the cap math (longs_MV + shorts_liab + new <= 95% eq)
        // traceable to specific tickers.
        var _notionalTxt = "—";
        if (Number.isFinite(_riskShareCount) && _riskShareCount > 0
            && Number.isFinite(_riskEntry) && _riskEntry > 0) {
          _notionalTxt = fmtUsd(_riskShareCount * _riskEntry);
        }
        // v8.1.2 -- partial-fill indicator on the shares cell. If the
        // position has any partial_fills (written by
        // broker/orders.py:partial_close_breakout when the engine
        // emits EXIT_PARTIAL on 1R touch), render a small "½@$X"
        // badge with a tooltip showing booked partial pnl.
        var _partialFills = Array.isArray(p.partial_fills) ? p.partial_fills : [];
        var _partialBadge = "";
        if (_partialFills.length > 0) {
          var _pf = _partialFills[_partialFills.length - 1];
          var _pfShares = Number(_pf && _pf.shares) || 0;
          var _pfPrice = Number(_pf && _pf.price) || 0;
          var _pfPnl = Number(_pf && _pf.pnl_dollars) || 0;
          var _pfTitle = "Partial fill taken at 1R: "
            + _pfShares + " sh @ $" + _pfPrice.toFixed(2)
            + " (booked $" + _pfPnl.toFixed(2) + "). "
            + "Runner riding to RR=2.5 target.";
          _partialBadge = ' <span class="partial-badge" title="'
            + escapeHtml(_pfTitle) + '">½@$'
            + _pfPrice.toFixed(2) + '</span>';
        }
        // v9.1.9 -- per-row chart-detail when the operator has the
        // position expanded (toggled via row click). The chart hydrates
        // through window.__tgRenderTickerChart, the same pipeline the
        // v10 Proximity matrix uses (one entry point so future chart
        // changes apply everywhere automatically).
        const _expanded = body.__posExpanded && body.__posExpanded.has(p.ticker);
        const _chartRow = _expanded
          ? '<tr class="pos-chart-row" data-pos-chart="' + escapeHtml(p.ticker) + '">'
            + '<td colspan="11" class="pos-chart-cell">'
            + '<div class="pos-chart-mount" data-chart-mount="' + escapeHtml(p.ticker) + '"></div>'
            + '</td></tr>'
          : '';
        /* Compact held: "67m" under 1h, "1h7m" at 1h+, no caption */
        var _heldSec = Number(p.held_seconds) || 0;
        var _heldMin = Math.round(_heldSec / 60);
        var _heldShort = _heldMin < 60
          ? _heldMin + 'm'
          : Math.floor(_heldMin/60) + 'h' + (_heldMin%60 > 0 ? (_heldMin%60) + 'm' : '');
        /* Morning vs EOD session badge */
        var _sessionBadge = p.eod
          ? '<span style="font-size:9px;color:#a78bfa;background:rgba(139,92,246,0.15);'
            + 'padding:1px 5px;border-radius:3px;margin-left:4px;font-weight:600">EOD</span>'
          : '<span style="font-size:9px;color:#6b7280;background:rgba(75,85,99,0.15);'
            + 'padding:1px 5px;border-radius:3px;margin-left:4px">Morning</span>';

        return `<tr data-pos-ticker="${escapeHtml(p.ticker)}" tabindex="0" role="button" aria-expanded="${_expanded ? 'true' : 'false'}" style="cursor:pointer">
          <td colspan="4">
            <div style="display:flex;align-items:baseline;gap:8px;flex-wrap:wrap">
              <span class="ticker">${escapeHtml(p.ticker)} <span class="mark ${markCls}" title="${escapeHtml(dotTitle)}">●</span></span>
              ${_sessionBadge}${phase !== 'A' ? phaseBadge : ''}
              <span class="${pnlCls}" style="font-weight:600;margin-left:auto">${pctTxt}</span>
              <span class="${pnlCls}">${fmtUsd(p.unrealized)}</span>
              <span style="color:#6b7280;font-size:11px">${_heldShort}</span>
            </div>
          </td>
          <td class="right" style="font-size:11px;color:#6b7280">${p.shares}${_partialBadge}</td>
          <td class="right" style="font-size:11px;color:#6b7280">${fmtPx(p.entry)}</td>
          <td class="right" style="font-size:11px;color:#6b7280">${fmtPx(p.mark)}</td>
          <td class="right" style="font-size:11px;color:#6b7280">${_notionalTxt}</td>
          <td class="right" style="font-size:11px;color:#6b7280">${fmtPx(eff)}${trailBadge}</td>
        </tr>${progressRow}${_chartRow}`;
      }).join("");
      // Only render the ORB table when there are actual ORB rows \u2014 an empty table
      // pushes the EOD section below an orphaned header, making positions appear
      // to "appear and disappear" on refresh. EOD table gets its own headers when standalone.
      var _posTableHeaders = '<thead><tr>' +
        '<th colspan="4">Ticker \u00b7 Session \u00b7 P&L \u00b7 Held</th>' +
        '<th class="right" title="Shares">Sh</th>' +
        '<th class="right" title="Entry price">Entry</th>' +
        '<th class="right" title="Current mark">Mark</th>' +
        '<th class="right" title="Notional at cost">Notional</th>' +
        '<th class="right" title="Effective stop">Stop</th>' +
        '</tr></thead>';
      if (positions.length > 0) {
        body.innerHTML = '<table>' + _posTableHeaders + '<tbody>' + rows + '</tbody></table>';
      } else {
        body.innerHTML = "";
      }

      if (_eodMainTickers.length > 0) {
        var _eodEtMin = __tgNowEtMinutes();
        var _eodWS = 15 * 60, _eodWE = 15 * 60 + 59;
        var _eodRows = _eodMainTickers.map(function(tk) {
          var ep = _eodMain[tk];
          var _sc = ep.side === "short" ? "side-short" : "side-long";
          var _sl = ep.side === "short" ? "SHORT" : "LONG";
          var _nt = fmtUsd(ep.shares * ep.entry_price);
          var _elapsed = Math.max(0, Math.min(59, _eodEtMin - _eodWS));
          var _pct = (_elapsed / 59) * 100;
          var _rem = 59 - _elapsed;
          var _nc = ep.side === "short" ? "eod-needle-short" : "eod-needle-long";
          var _bar =
            '<tr class="pos-progress-row eod-time-bar" data-pos-ticker="' + escapeHtml(tk) + '">' +
              '<td colspan="11" class="pos-progress-cell">' +
                '<div class="pos-progress eod-progress">' +
                  '<div class="pos-progress-track">' +
                    '<div class="pos-progress-zone eod-elapsed" style="left:0%;width:' + _pct.toFixed(1) + '%;border-radius:5px 0 0 5px"></div>' +
                    '<div class="pos-progress-zone eod-remain" style="left:' + _pct.toFixed(1) + '%;width:' + (100 - _pct).toFixed(1) + '%;border-radius:0 5px 5px 0"></div>' +
                    '<span class="pos-progress-needle ' + _nc + '" style="left:' + _pct.toFixed(1) + '%">' +
                      '<span class="needle-label">' + _elapsed + 'm</span>' +
                    '</span>' +
                  '</div>' +
                  '<div class="pos-progress-meta">' +
                    '<span class="pp-meta-left">15:00 entry</span>' +
                    '<span class="pp-meta-center">' + _rem + 'm to EOD exit</span>' +
                    '<span class="pp-meta-right">15:59 exit</span>' +
                  '</div>' +
                '</div>' +
              '</td>' +
            '</tr>';
          var _markTxt = Number.isFinite(ep.current_price) ? fmtPx(ep.current_price) : '—';
          var _unrNum = ep.unrealized_pnl;
          var _unrTxt = Number.isFinite(_unrNum) ? fmtUsd(_unrNum) : '—';
          var _pctTxt = Number.isFinite(ep.unrealized_pct) ? fmtPct(ep.unrealized_pct) : '—';
          var _unrCls = Number.isFinite(_unrNum) ? (_unrNum >= 0 ? 'delta-up' : 'delta-down') : '';
          var _stopTxt = (ep.stop_price != null && Number.isFinite(ep.stop_price)) ? fmtPx(ep.stop_price) : '—';
          return '<tr data-pos-ticker="' + escapeHtml(tk) + '">' +
            '<td><span class="ticker">' + escapeHtml(tk) + '</span><span class="eod-badge">EOD</span></td>' +
            '<td><span class="' + _sc + '">' + _sl + '</span></td>' +
            '<td class="right">' + ep.shares + '</td>' +
            '<td class="right">' + fmtPx(ep.entry_price) + '</td>' +
            '<td class="right">' + _markTxt + '</td>' +
            '<td class="right">' + _nt + '</td>' +
            '<td class="right">' + _stopTxt + '</td>' +
            '<td class="right">—</td>' +
            '<td class="right ' + _unrCls + '">' + _unrTxt + '</td>' +
            '<td class="right ' + _unrCls + '">' + _pctTxt + '</td>' +
            '<td class="right">' + fmtHeld(ep.entry_iso) + '</td>' +
          '</tr>' + _bar;
        }).join("");
        // When ORB positions also present: add a separator; EOD table is a
        // headerless continuation. When standalone: give it the shared headers
        // so the operator sees the column labels.
        if (positions.length > 0) {
          body.innerHTML += '<div class="eod-section-sep"></div>' +
            '<table class="eod-pos-table"><tbody>' + _eodRows + '</tbody></table>';
        } else {
          body.innerHTML = '<table class="eod-pos-table">' + _posTableHeaders +
            '<tbody>' + _eodRows + '</tbody></table>';
        }
      }
    }

    // v9.1.9 -- click a position row to toggle an inline intraday chart
    // beneath it. Mirrors the v10 Proximity expansion pattern; uses the
    // shared window.__tgRenderTickerChart hydration pipeline so any
    // future chart change (e.g. v9.1.9's RTH-only window) propagates
    // automatically. Pre-v9.1.9 the click scrolled to the legacy Tiger
    // Sovereign Permit Matrix, which has been hidden under body.v10-live
    // since v7.27.0 -- so the old handler was a dead-end UX.
    if (!body.__posExpanded) body.__posExpanded = new Set();
    if (!body.__posClickWired) {
      body.addEventListener("click", function _posRowClick(ev) {
        // Ignore clicks inside the progress / chart detail rows --
        // those are non-interactive surfaces beneath the main row.
        if (ev.target.closest("tr.pos-progress-row")) return;
        if (ev.target.closest("tr.pos-chart-row")) return;
        const tr = ev.target.closest("tr[data-pos-ticker]");
        if (!tr) return;
        const ticker = tr.getAttribute("data-pos-ticker");
        if (!ticker) return;
        if (body.__posExpanded.has(ticker)) {
          body.__posExpanded.delete(ticker);
        } else {
          body.__posExpanded.add(ticker);
        }
        // Re-render via the exposed entry point so the chart row is
        // inserted/removed deterministically; alternative would be DOM
        // surgery here but a re-render guarantees the chart hydration
        // path runs exactly once per state change.
        if (typeof window !== "undefined"
            && typeof window.__tgRenderPositions === "function"
            && window.__tgLastState) {
          window.__tgRenderPositions(window.__tgLastState, sl);
        }
      });
      body.addEventListener("keydown", function _posRowKey(ev) {
        if (ev.key !== "Enter" && ev.key !== " ") return;
        const tr = ev.target.closest("tr[data-pos-ticker]");
        if (!tr) return;
        ev.preventDefault();
        tr.click();
      });
      body.__posClickWired = true;
    }

    // v9.1.9 -- hydrate every inline chart mount via the shared
    // pipeline. Re-runs on each render; the underlying cache in
    // _pmtxHydrateIntradayCharts is keyed by ticker so a re-render
    // mid-fetch reuses the in-flight payload instead of double-fetching.
    try {
      const _mountFn = (typeof window !== "undefined") && window.__tgRenderTickerChart;
      if (typeof _mountFn === "function") {
        body.querySelectorAll('.pos-chart-row [data-chart-mount]').forEach(function (mount) {
          const tk = mount.getAttribute("data-chart-mount");
          if (tk) _mountFn(tk, mount);
        });
      }
    } catch (e) { /* never break the positions renderer */ }

    // v7.89.0 -- port-strip footer below the positions table is
    // retired; Equity is shown in the KPI row above the table and
    // per-position invested / short-liability dollars are in the
    // Notional column. See index.html v7.89.0 reorder.
  }

  // v5.18.0 — the standalone Main-tab Proximity card was retired and its
  // data (live price + % distance to nearest OR boundary) was folded into
  // the Permit Matrix Price·Distance column (see _pmtxBuildRow below).
  // Val/Gene tabs still render their own per-executor proximity strip
  // via execRenderProximity in IIFE-2 — those panels are portfolio-only
  // and don't carry a Permit Matrix.

  // v3.4.30 — time may already arrive pre-formatted as "09:11 CDT"
  // (current server) or as an ISO-8601 string; accept both.
  // NOTE: can't branch on .includes("T") because "CDT"/"EST" etc.
  // contain T; branch on the ISO shape "YYYY-MM-DDTHH:MM" instead.
  function fmtTradeTime(rawT) {
    const s = (rawT || "").toString();
    if (!s) return "—";
    const iso = s.match(/^\d{4}-\d{2}-\d{2}T(\d{2}:\d{2})/);
    if (iso) return iso[1];
    const hm = s.match(/^\d{1,2}:\d{2}/);
    return hm ? hm[0] : s;
  }

  // v5.5.7 — compute the daily summary for the Today's Trades card.
  // Opens are BUY (long entry) or SHORT (short entry); closes are
  // SELL (long exit) or COVER (short exit). Pre-v5.5.7 only BUY/SELL
  // were counted, so a SHORT+COVER pair rendered "0 opens 0 closes
  // realized —" even though the COVER row was visible. The realized
  // P&L branch applies to any close action that carries a numeric pnl.
  function computeTradesSummary(trades) {
    let opens = 0, closes = 0, wins = 0, realized = 0, have_pnl = 0;
    for (const t of (trades || [])) {
      const act = (t.action || "").toUpperCase();
      const isOpen = (act === "BUY" || act === "SHORT");
      const isClose = (act === "SELL" || act === "COVER");
      if (isOpen) opens += 1;
      else if (isClose) {
        closes += 1;
        if (typeof t.pnl === "number" && isFinite(t.pnl)) {
          realized += t.pnl;
          have_pnl += 1;
          if (t.pnl > 0) wins += 1;
        }
      }
    }
    const win_rate = have_pnl > 0 ? (wins / have_pnl) : null;
    return { opens, closes, wins, realized, have_pnl, win_rate };
  }

  // v3.4.31 — Today's Trades card.
  // Desktop: one row per trade on a 6-col grid (time, sym, action
  // chip, qty, price, trailing cell). Trailing cell shows cost on
  // BUY rows (shares * price) and realized P&L ($ + %) on SELL rows.
  // On phones the row collapses to two stacked lines (see 640px
  // media block). Summary header sits above the rows; a chip in the
  // card head shows the running realized P&L for the day.
  function renderTrades(s, sl) {
    const trades = sl.trades || [];
    $("trades-count").textContent = `· ${trades.length}`;
    const summary = computeTradesSummary(trades);

    // Header chip — running realized $.
    const chip = $("trades-realized");
    if (chip) {
      if (summary.have_pnl > 0) {
        chip.textContent = fmtUsd(summary.realized);
        chip.className = "chip " + (summary.realized > 0 ? "chip-ok" : (summary.realized < 0 ? "chip-down" : "chip-neut"));
      } else {
        chip.textContent = "—";
        chip.className = "chip chip-neut";
      }
    }

    // Inline summary line above the table.
    const sumEl = $("trades-summary");
    if (sumEl) {
      if (!trades.length) {
        sumEl.innerHTML = '<span class="ts-seg" title="No buy or sell fills have been recorded today">No fills yet today.</span>';
      } else {
        const realCls = summary.have_pnl === 0 ? "na"
                      : (summary.realized > 0 ? "up" : (summary.realized < 0 ? "down" : ""));
        const realTxt = summary.have_pnl === 0 ? "—" : fmtUsd(summary.realized);
        const wrTxt   = summary.win_rate === null ? "—"
                      : (Math.round(summary.win_rate * 100) + "%");
        sumEl.innerHTML =
          `<span class="ts-seg" title="Number of opening fills today (BUY for long, SHORT for short)"><span class="ts-val">${summary.opens}</span> open${summary.opens===1?"":"s"}</span>` +
          `<span class="ts-seg" title="Number of closing fills today (SELL for long, COVER for short)"><span class="ts-val">${summary.closes}</span> close${summary.closes===1?"":"s"}</span>` +
          `<span class="ts-seg" title="Sum of realized P&L from closed pairs today, after commissions">realized <span class="ts-val ${realCls}">${realTxt}</span></span>` +
          `<span class="ts-seg" title="Win rate among closed pairs today (winners / total closed)">win <span class="ts-val">${wrTxt}</span></span>`;
      }
    }

    const body = $("trades-body");
    if (!trades.length) {
      body.innerHTML = `<div class="empty">No trades yet today.</div>`;
      return;
    }

    const rows = trades.map((t) => {
      const tm   = fmtTradeTime(t.time || t.entry_time);
      const act  = (t.action || "").toUpperCase();
      // v5.5.7 — classify by open vs close, not strictly BUY/SELL.
      // SHORT entries pair with COVER exits; treating only BUY/SELL as
      // tradable actions hid realized pnl on COVER rows.
      // v8.1.2 -- PARTIAL_SELL / PARTIAL_COVER are HALF-closes
      // (booked realized P&L on half the position; position stays
      // open). Treat as "close" for the chip/color but mark with a
      // ½ glyph + tooltip so operator can distinguish.
      const isPartial = (act === "PARTIAL_SELL" || act === "PARTIAL_COVER");
      const isOpen  = (act === "BUY" || act === "SHORT");
      const isClose = (act === "SELL" || act === "COVER" || isPartial);
      const side  = t.side || "LONG";
      const sym   = t.ticker || "—";
      const shares = t.shares;
      // v8.3.11 -- pick the action-relevant price. For close actions
      // (SELL / COVER / PARTIAL_*) prefer exit_price; for opens
      // prefer entry_price. The in-memory short_trade_history COVER
      // row has no "price" field (only entry_price + exit_price),
      // so the old chain `t.price ?? t.entry_price ?? t.exit_price`
      // fell through to entry_price and displayed the wrong number
      // for CLOSE rows (operator screenshot: AMZN COVER showing
      // $264.05 entry instead of $265.12 cover).
      const px = isClose
        ? (t.exit_price ?? t.price ?? t.entry_price)
        : (t.entry_price ?? t.price ?? t.exit_price);

      // Action chip — open (green) / close (red). Symbol still
      // carries LONG/SHORT colour coding to avoid double-cueing.
      // Partial fills get an amber tone (between win green and loss
      // red) since they're half-closes booking profit on a still-
      // open position.
      const actCls = isPartial ? "act-partial" :
                     (isClose ? "act-sell" : "act-buy");
      const actLbl = isPartial
        ? (act === "PARTIAL_SELL" ? "½ SELL" : "½ COVER")
        : (act || (side === "SHORT" ? "SHORT" : "LONG"));

      // v4.2.1 — tail column (between action and unit price):
      //   open  → total cost, subdued
      //   close → signed pnl + matching-colour pnl %
      let tailHtml = "\u2014";
      if (isOpen) {
        const cost = (typeof t.cost === "number" && isFinite(t.cost))
          ? t.cost
          : ((typeof shares === "number" && typeof px === "number") ? shares * px : null);
        tailHtml = cost !== null
          ? `<span class="trade-cost">${fmtUsd(cost)}</span>`
          : `<span class="trade-cost">\u2014</span>`;
      } else if (isClose) {
        const pnl   = (typeof t.pnl === "number" && isFinite(t.pnl)) ? t.pnl : null;
        const pnlPct = (typeof t.pnl_pct === "number" && isFinite(t.pnl_pct)) ? t.pnl_pct : null;
        if (pnl !== null) {
          const pnlCls = pnl > 0 ? "up" : (pnl < 0 ? "down" : "");
          const sign = pnl > 0 ? "+" : "";
          const pctStr = pnlPct !== null ? ` <span class="pnl-pct ${pnlCls}">${fmtPct(pnlPct)}</span>` : "";
          tailHtml = `<span class="trade-pnl ${pnlCls}">${sign}${fmtUsd(pnl)}${pctStr}</span>`;
        } else {
          tailHtml = `<span class="trade-pnl">\u2014</span>`;
        }
      }

      return `<div class="trade-row" data-act="${escapeHtml(act)}" data-sym="${escapeHtml(sym)}">
        <span class="tr-time">${escapeHtml(tm)}</span>
        <span class="tr-sym ticker">${escapeHtml(sym)}</span>
        <span class="tr-qty">${shares ?? "\u2014"}</span>
        <span class="tr-act"><span class="act-badge ${actCls}">${escapeHtml(actLbl)}</span></span>
        <span class="tr-tail">${tailHtml}</span>
        <span class="tr-price">${fmtPx(px)}</span>
      </div>`;
    }).join("");

    body.innerHTML = `<div class="trades-list">${rows}</div>`;
  }


  // v5.17.0 — Legacy v5.13.2 Tiger Sovereign Phase 1–4 renderer removed.
  // The Observer panel, Sovereign Regime Shield panel, Volume Gate flag
  // pill, Gates · entry checks panel, and "Eye of the Tiger" panel were
  // all retired in this version. Their surface area is now folded into
  // the Weather Check banner + Permit Matrix below, which read the same
  // tiger_sovereign block in /api/state but pivot the data to a per-Titan
  // row layout. The regime/observer blocks remain in /api/state for any
  // external consumer but are no longer drawn on the dashboard.

  // ─── Permit Matrix helpers ───────────────────────────────────────
  // Tri-state gate cell: pass / fail / pending. Used by both the
  // desktop table and the mobile card stack.
  // v5.19.2 \u2014 OR-high / OR-low boundary labels render as compact
  // ORH / ORL in matrix cells. The server's /api/state contract still
  // emits the full "OR-high" / "OR-low" strings (pinned by
  // tests/test_dashboard_state_v5_13_2.py); abbreviation is purely
  // client-side so the API surface is unchanged.
  function _pmtxAbbrevBoundary(label) {
    if (!label) return "";
    const s = String(label);
    if (s === "OR-high") return "ORH";
    if (s === "OR-low")  return "ORL";
    return s;
  }

  // v5.19.2 \u2014 DI gate is side-aware. The server's entry1_di field is
  // already the side-correct reading (DI+ for LONG, DI\u2212 for SHORT,
  // see v5_13_2_snapshot._phase3_row). The header reads DI\u00b1; the
  // tooltip names the actual side and value when known.
  function _pmtxDiTooltip(p3, longPermit, shortPermit) {
    let sideLabel = "DI\u00b1";
    if (longPermit && !shortPermit) sideLabel = "DI+";
    else if (shortPermit && !longPermit) sideLabel = "DI\u2212";
    let tip = sideLabel + " on 5m bars above 25";
    if (p3 && typeof p3.entry1_di === "number") {
      tip += " \u2014 last reading: " + _pmtxNum(p3.entry1_di, 1);
    }
    return tip;
  }

  function _pmtxGateCell(state, label) {
    let cellCls = "pmtx-gate-pend";
    let glyph = "\u2212"; // pending
    if (state === true)  { cellCls = "pmtx-gate-pass"; glyph = "\u2713"; }
    else if (state === false) { cellCls = "pmtx-gate-fail"; glyph = "\u2717"; }
    else if (state === "warn") { cellCls = "pmtx-gate-warn"; glyph = "!"; }
    const title = label ? ` title="${escapeHtml(label)}"` : "";
    return `<span class="pmtx-gate ${cellCls}"${title}>${glyph}</span>`;
  }

  // v5.20.8 \u2014 Authority column (formerly 5m DI\u00b1) now reflects
  // Section-I permit alignment to match the rewired Authority card. The
  // cell goes green (pass) when at least one of long_open / short_open
  // is true, red (fail) when both are explicitly false, pending when
  // the booleans are missing or section_i_permit is unavailable.
  function _pmtxAuthorityCell(sip) {
    if (!sip) return null;
    const hasLong  = (typeof sip.long_open === "boolean");
    const hasShort = (typeof sip.short_open === "boolean");
    if (!hasLong && !hasShort) return null;
    const lo = hasLong  ? sip.long_open  : false;
    const so = hasShort ? sip.short_open : false;
    if (lo || so) return true;
    return false;
  }

  // v5.31.5 \u2014 per-stock Weather column glyph. Lives at table position 2,
  // between Titan and Boundary. Shows `x` when neither side has any kind of
  // permit (global QQQ closed AND local override would not flip it open).
  // Otherwise shows a green up-arrow for long-aligned local weather, a
  // green down-arrow for short-aligned, or an em-dash while the data is
  // still warming up. The directional arrow + alignment is sourced from
  // the per-ticker weather block (per_ticker_v510[t].weather.direction).
  function _pmtxWeatherCell(ptv, longPermit, shortPermit) {
    const wx = (ptv && ptv.weather) || null;
    const dir = (wx && typeof wx.direction === "string") ? wx.direction : "flat";
    const div = !!(wx && wx.divergence);
    const tooltipBase = div
      ? "Local weather diverges from global QQQ direction. "
      : "";
    // v6.0.0 \u2014 small star marker overlaid on the glyph when local
    // direction != global direction. Lets operators scan the matrix
    // and spot contrarian-trending tickers without expanding cards.
    const star = div
      ? '<sup class="pmtx-wx-star" aria-hidden="true">*</sup>'
      : "";
    const divClass = div ? " pmtx-wx-div" : "";
    if (!longPermit && !shortPermit && dir === "flat") {
      return '<span class="pmtx-wx pmtx-wx-none' + divClass + '" title="' + escapeHtml(tooltipBase + "No permit: global QQQ closed both sides and the local override is not aligned") + '">x' + star + '</span>';
    }
    if (dir === "up") {
      return '<span class="pmtx-wx pmtx-wx-up' + divClass + '" title="' + escapeHtml(tooltipBase + "Local weather is long-aligned (5m close past EMA9 OR last past AVWAP, with 1m DI confirming)") + '">\u2191' + star + '</span>';
    }
    if (dir === "down") {
      return '<span class="pmtx-wx pmtx-wx-down' + divClass + '" title="' + escapeHtml(tooltipBase + "Local weather is short-aligned (5m close past EMA9 OR last past AVWAP, with 1m DI confirming)") + '">\u2193' + star + '</span>';
    }
    return '<span class="pmtx-wx pmtx-wx-flat' + divClass + '" title="' + escapeHtml((div ? tooltipBase : "") + "Local weather is flat or warming up \u2014 not enough structure or DI confirmation yet") + '">\u2014' + star + '</span>';
  }

  // v6.0.0 \u2014 Mini-chart sparkline for the collapsed row's Trend
  // column. Renders an inline SVG polyline covering today's 1m closes
  // (downsampled server-side to \u2264 60 points). Stroke goes green
  // when last > open, red when last < open, neutral when missing.
  // Width 80px / height 24px keeps it scannable across the matrix
  // without disrupting row height. The tooltip surfaces hi/lo/last
  // and the open price so an operator can quickly read the day so far.
  function _pmtxMiniChartCell(ptv) {
    const mc = ptv && ptv.mini_chart;
    if (!mc || !Array.isArray(mc.points) || mc.points.length < 2) {
      return '<span class="pmtx-mini pmtx-mini-empty" title="No intraday closes yet">\u2014</span>';
    }
    const pts = mc.points;
    const hi = (typeof mc.hi === "number") ? mc.hi : Math.max.apply(null, pts);
    const lo = (typeof mc.lo === "number") ? mc.lo : Math.min.apply(null, pts);
    const open = (typeof mc.open === "number") ? mc.open : pts[0];
    const last = (typeof mc.last === "number") ? mc.last : pts[pts.length - 1];
    const span = Math.max(hi - lo, 0.0001);
    const W = 80, H = 24, padY = 2;
    const stepX = (W - 2) / (pts.length - 1);
    let d = "";
    for (let i = 0; i < pts.length; i++) {
      const x = 1 + i * stepX;
      const y = padY + (1 - (pts[i] - lo) / span) * (H - padY * 2);
      d += (i === 0 ? "M" : "L") + x.toFixed(1) + " " + y.toFixed(1) + " ";
    }
    let cls = "pmtx-mini-flat";
    if (last > open) cls = "pmtx-mini-up";
    else if (last < open) cls = "pmtx-mini-down";
    const tip = "Open " + open.toFixed(2)
      + " \u00b7 Last " + last.toFixed(2)
      + " \u00b7 Hi " + hi.toFixed(2)
      + " \u00b7 Lo " + lo.toFixed(2)
      + " (" + pts.length + " pts)";
    return '<svg class="pmtx-mini ' + cls + '" viewBox="0 0 ' + W + ' ' + H
      + '" width="' + W + '" height="' + H + '" preserveAspectRatio="none"'
      + ' role="img" aria-label="' + escapeHtml(tip) + '">'
      + '<title>' + escapeHtml(tip) + '</title>'
      + '<path d="' + d.trim() + '" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round" stroke-linecap="round"></path>'
      + '</svg>';
  }

  function _pmtxAuthorityTooltip(sip) {
    if (!sip) return "Authority: section_i_permit unavailable";
    const lo = (typeof sip.long_open === "boolean") ? (sip.long_open ? "yes" : "no") : "?";
    const so = (typeof sip.short_open === "boolean") ? (sip.short_open ? "yes" : "no") : "?";
    const sa = (typeof sip.sovereign_anchor_open === "boolean")
      ? (sip.sovereign_anchor_open ? "yes" : "no")
      : "?";
    return "Authority (Section-I permit alignment): long=" + lo
      + " \u00b7 short=" + so + " \u00b7 sov.anchor=" + sa
      + ". Cell is green when long or short permit is open.";
  }

  function _pmtxNum(v, digits) {
    if (v === null || v === undefined || !isFinite(v)) return "\u2014";
    return Number(v).toFixed(digits === undefined ? 2 : digits);
  }

  function _pmtxMoney(v) {
    if (v === null || v === undefined || !isFinite(v)) return "\u2014";
    const n = Number(v);
    const sign = n >= 0 ? "+" : "\u2212";
    return sign + "$" + Math.abs(n).toFixed(2);
  }

  // v5.20.3 \u2014 expanded-row component card grid. Replaces the
  // verbatim v15.0 spec <dl> with a responsive 3\u20134-cards-per-row
  // grid: each card is a single pipeline component (Phase 1/2/3, an
  // alarm, or the strike counter) showing a short description plus
  // current state (status badge + numeric value). Operators reading
  // the row no longer need to mentally cross-reference live data with
  // the verbatim spec text \u2014 the card surfaces both inline.
  function _pmtxComponentGrid(d) {
    // Phase 1 \u00b7 Weather \u2014 the QQQ regime + AVWAP gate that controls
    // long/short permits.
    let p1State; let p1Val;
    if (d.longPermit && d.shortPermit) { p1State = "pass"; p1Val = "long+short"; }
    else if (d.longPermit)             { p1State = "pass"; p1Val = "long permit"; }
    else if (d.shortPermit)            { p1State = "pass"; p1Val = "short permit"; }
    else                                { p1State = "fail"; p1Val = "no permit"; }

    // Phase 2 \u00b7 Boundary \u2014 two consecutive 1m closes through the OR.
    // v6.1.1 \u2014 surface the v6.1.0 ATR-normalized OR-break state. When
    // the gate is enabled the val text shows the active k-multiplier;
    // when dormant (default in v6.1.0) it shows "OR only" to make the
    // legacy entry path explicit. The card colour still reflects the raw
    // ORB pass/fail state so existing semantics are preserved.
    const _v610OrEnabled = !!(d.v610Flags && d.v610Flags.or_break_enabled);
    const _v610OrK = (d.v610Flags && typeof d.v610Flags.or_break_k === "number") ? d.v610Flags.or_break_k : 0.0;
    const _v610LateOr = !!(d.v610Flags && d.v610Flags.late_or_enabled);
    let _orBreakSuffix = "";
    if (_v610OrEnabled && _v610OrK > 0) {
      _orBreakSuffix = " \u00b7 \u2265OR+" + _v610OrK.toFixed(2) + "\u00d7ATR";
      if (_v610LateOr) _orBreakSuffix += " \u00b7 late-OR";
    } else {
      _orBreakSuffix = " \u00b7 OR only";
    }
    // v6.2.0 \u2014 fast-boundary suffix. When the time-conditional 1-bar
    // hold is enabled, append "1-bar pre-CUTOFF" so operators can see the
    // active relaxation. When disabled, render "2-bar hold" to make the
    // legacy spec-strict path explicit.
    const _v620FastBoundary = !!(d.v620Flags && d.v620Flags.fast_boundary_enabled);
    const _v620FastBoundaryCutoff = (d.v620Flags && typeof d.v620Flags.fast_boundary_cutoff_et === "string")
      ? d.v620Flags.fast_boundary_cutoff_et : "10:30";
    const _fastBoundarySuffix = _v620FastBoundary
      ? (" \u00b7 1-bar pre-" + _v620FastBoundaryCutoff)
      : " \u00b7 2-bar hold";
    let p2bState; let p2bVal;
    if (d.orb === true)       { p2bState = "pass"; p2bVal = "two consec" + _orBreakSuffix + _fastBoundarySuffix; }
    else if (d.orb === false) { p2bState = "fail"; p2bVal = "hold" + _orBreakSuffix + _fastBoundarySuffix; }
    else                       { p2bState = "pend"; p2bVal = "\u2014" + _orBreakSuffix + _fastBoundarySuffix; }

    // Phase 2 \u00b7 Volume \u2014 1m volume \u2265 100% of 55-bar avg.
    let p2vState; let p2vVal;
    const vs = String(d.volStatus || "").toUpperCase();
    if (d.vol === true)        { p2vState = "pass"; p2vVal = vs || "PASS"; }
    else if (d.vol === false)  { p2vState = "fail"; p2vVal = vs || "FAIL"; }
    else if (d.vol === "warn") { p2vState = "warn"; p2vVal = vs || "COLD"; }
    else                        { p2vState = "pend"; p2vVal = vs || "\u2014"; }

    // Phase 3 \u00b7 Authority \u2014 Section-I permit alignment.
    // v5.20.8: state and value now reflect the rewired card content
    // (long_open / short_open from section_i_permit) rather than the
    // legacy 5m DI\u00b1 gate. Card goes green (pass) when at least
    // one side has its permit open; red (fail) when both sides are
    // closed; pend when the booleans are missing. The val text
    // mirrors the Weather card style: 'long+short' / 'long' /
    // 'short' / 'none'.
    let p3aState; let p3aVal;
    const _sip = d.sectionIPermit || {};
    const _hasLong  = (typeof _sip.long_open === "boolean");
    const _hasShort = (typeof _sip.short_open === "boolean");
    if (_hasLong || _hasShort) {
      const _lo = _hasLong ? _sip.long_open : false;
      const _so = _hasShort ? _sip.short_open : false;
      if (_lo && _so)        { p3aState = "pass"; p3aVal = "long+short"; }
      else if (_lo)          { p3aState = "pass"; p3aVal = "long"; }
      else if (_so)          { p3aState = "pass"; p3aVal = "short"; }
      else                   { p3aState = "fail"; p3aVal = "none"; }
    } else {
      p3aState = "pend"; p3aVal = "\u2014";
    }
    // v6.1.1 \u2014 EMA-confirm + lunch-suppression state surfaced as a
    // suffix on the Phase 3 Authority card. Mirrors the OR-break suffix
    // pattern on the Phase 2 Boundary card. Defaults make missing flags
    // render as legacy single-bar / no-window behaviour.
    const _v610EmaConfirm = !!(d.v610Flags && d.v610Flags.ema_confirm_enabled);
    const _v610Lunch      = !!(d.v610Flags && d.v610Flags.lunch_suppression_enabled);
    let _emaSuffix = "";
    if (_v610EmaConfirm) {
      _emaSuffix = " \u00b7 EMA 2-bar";
      if (_v610Lunch) _emaSuffix += " \u00b7 lunch \u2713";
    } else if (_v610Lunch) {
      _emaSuffix = " \u00b7 lunch \u2713";
    }
    if (_emaSuffix) p3aVal = p3aVal + _emaSuffix;

    // Phase 3 \u00b7 Momentum \u2014 5m ADX > 20 (proxied by entry1_fired).
    // v6.2.0 \u2014 append the active Entry-1 DI threshold so operators can
    // see the new looser hurdle (default 22 since v6.2.0; previously 25).
    let p3mState; let p3mVal;
    if (d.adx === true)       { p3mState = "pass"; p3mVal = "fired"; }
    else if (d.adx === false) { p3mState = "fail"; p3mVal = "blocked"; }
    else                       { p3mState = "pend"; p3mVal = "\u2014"; }
    const _v620Di = (d.v620Flags && typeof d.v620Flags.entry1_di_threshold === "number")
      ? d.v620Flags.entry1_di_threshold : null;
    if (_v620Di !== null) {
      p3mVal = p3mVal + " \u00b7 DI\u2265" + _v620Di.toFixed(0);
    }

    // Alarm A1 Loss (vAA-1 SENT-A_LOSS) \u00b7 Per-position $ stop. Only meaningful when in position.
    const sen = (d.p4 && d.p4.sentinel) || {};
    const _aLossObj = (sen.a_loss && typeof sen.a_loss === "object") ? sen.a_loss : null;
    const a1 = _aLossObj ? ((typeof _aLossObj.pnl === "number") ? _aLossObj.pnl : null)
                         : ((typeof sen.a1_pnl === "number") ? sen.a1_pnl : null);
    const a1Th = _aLossObj ? ((typeof _aLossObj.threshold === "number") ? _aLossObj.threshold : -500)
                           : ((typeof sen.a1_threshold === "number") ? sen.a1_threshold : -500);
    let alAState; let alAVal;
    if (!d.pos)               { alAState = "off"; alAVal = "no pos"; }
    else if (a1 === null)      { alAState = "pend"; alAVal = "\u2014"; }
    else if (a1 <= a1Th)       { alAState = "trip"; alAVal = _pmtxMoney(a1); }
    else                        { alAState = "safe"; alAVal = _pmtxMoney(a1); }

    // Alarm A2 Flash (vAA-1 SENT-A_FLASH) \u00b7 1-min adverse velocity.
    // New key: a_flash.velocity_pct (ratio, e.g. -0.013 = -1.3% adverse).
    // Legacy a2_velocity was a per-second rate \u2014 units differ, so only
    // show the new key; fall back to pend when the new sub-dict is absent.
    const _aFlashObj = (sen.a_flash && typeof sen.a_flash === "object") ? sen.a_flash : null;
    const a2VelPct = _aFlashObj ? ((typeof _aFlashObj.velocity_pct === "number") ? _aFlashObj.velocity_pct : null) : null;
    const a2ThPct  = _aFlashObj ? ((typeof _aFlashObj.threshold_pct === "number") ? _aFlashObj.threshold_pct : -0.01) : -0.01;
    const a2Triggered = _aFlashObj ? !!_aFlashObj.triggered : false;
    let alBState; let alBVal;
    if (!d.pos)                 { alBState = "off";  alBVal = "no pos"; }
    else if (a2VelPct === null) { alBState = "pend"; alBVal = "\u2014"; }
    else if (a2Triggered)       { alBState = "trip"; alBVal = _pmtxNum(a2VelPct * 100, 2) + "%"; }
    else                         { alBState = "safe"; alBVal = _pmtxNum(a2VelPct * 100, 2) + "%"; }

    // Position \u00b7 Strike count (max 3 per ticker per day).
    const su = d.strikesUsed || 0;
    let posState; let posVal;
    if (d.pos)            { posState = "inpos"; posVal = su + "/3 \u00b7 in pos"; }
    else if (su >= 3)      { posState = "locked"; posVal = "3/3 \u00b7 locked"; }
    else if (su > 0)       { posState = "used"; posVal = su + "/3 used"; }
    else                    { posState = "idle"; posVal = "0/3 \u00b7 idle"; }

    // v5.20.5 \u2014 helpers to format metric rows (key/value) beneath
    // each card state. Null-safe: missing values render as a dim dash so
    // the row layout remains stable while the data is still warming up.
    function _fmtNum(v, digits) {
      if (v === null || v === undefined || (typeof v === "number" && !isFinite(v))) return null;
      const n = Number(v);
      if (!isFinite(n)) return null;
      return n.toFixed(typeof digits === "number" ? digits : 2);
    }
    function _fmtPct(v, digits) {
      const s = _fmtNum(v, digits);
      return s === null ? null : (s + "%");
    }
    function _fmtInt(v) {
      if (v === null || v === undefined) return null;
      const n = Number(v);
      if (!isFinite(n)) return null;
      return String(Math.trunc(n));
    }
    function _metricRow(label, value) {
      const cls = (value === null || value === undefined || value === "")
        ? "pmtx-comp-metric-row pmtx-comp-metric-empty"
        : "pmtx-comp-metric-row";
      const v = (value === null || value === undefined || value === "") ? "\u2014" : value;
      return '<div class="' + cls + '">'
        +     '<span class="pmtx-comp-metric-key">' + escapeHtml(label) + '</span>'
        +     '<span class="pmtx-comp-metric-val">' + escapeHtml(String(v)) + '</span>'
        +   '</div>';
    }
    function _metricsHtml(rows) {
      if (!rows || !rows.length) return "";
      const inner = rows.map((r) => _metricRow(r[0], r[1])).join("");
      return '<div class="pmtx-comp-metrics">' + inner + '</div>';
    }

    // Source data for new card metric rows (v5.20.5).
    const ptv = d.ptv510 || {};
    const ppv = d.ppv510 || {};
    const reg = d.regimeBlock || {};
    const sip = d.sectionIPermit || {};
    const di = ptv.di || {};
    const vb = ptv.vol_bucket || {};
    const bh = ptv.boundary_hold || {};
    const sb = ppv.sovereign_brake || {};
    const vf = ppv.velocity_fuse || {};
    const stk = ppv.strikes || {};

    // v5.20.6 — Weather card sources QQQ price/EMA9/AVWAP from
    // section_i_permit (the only place the dashboard ships them).
    // Earlier wiring read reg.qqq_* fields that don't exist on the
    // top-level regime block, so every row rendered as a dim em dash.
    const p1Metrics = _metricsHtml([
      ["QQQ price",    _fmtNum(sip.qqq_current_price, 2)],
      ["QQQ 5m close", _fmtNum(sip.qqq_5m_close, 2)],
      ["QQQ 5m EMA9",  _fmtNum(sip.qqq_5m_ema9, 2)],
      ["QQQ AVWAP",    _fmtNum(sip.qqq_avwap_0930, 2)],
      ["Breadth",      reg.breadth || null],
      ["RSI regime",   reg.rsi_regime || null],
    ]);

    // v5.31.5 \u2014 Per-stock Weather card. Mirrors the Phase-1 global
    // Weather card but reads the ticker's own 5m EMA9 / AVWAP / DI.
    // State: pass = local direction is decisively up or down; warn =
    // local direction diverges from global QQQ; pend = flat / warming.
    // The card surfaces the same numeric inputs the local-override
    // gate uses, so the operator can reason about why an override
    // fired (or didn't) without combing the logs.
    const wx = (d.ptv510 && d.ptv510.weather) || {};
    const _wxDir = (typeof wx.direction === "string") ? wx.direction : "flat";
    const _wxDiv = !!wx.divergence;
    const _wxGlobal = (typeof wx.global_direction === "string") ? wx.global_direction : null;
    let pLwState; let pLwVal;
    if (_wxDir === "up")        { pLwState = _wxDiv ? "warn" : "pass"; pLwVal = _wxDiv ? "long (diverges)" : "long"; }
    else if (_wxDir === "down") { pLwState = _wxDiv ? "warn" : "pass"; pLwVal = _wxDiv ? "short (diverges)" : "short"; }
    else                         { pLwState = "pend"; pLwVal = "flat"; }
    // v6.2.0 — local OR-break leg suffix. When the divergence override
    // path admits an OR-break leg (ticker has cleared OR by k×ATR even
    // when QQQ permit closes), surface that as a card-text decoration so
    // operators see why the override may fire on a divergence.
    const _v620LocalOrBreak = !!(d.v620Flags && d.v620Flags.local_or_break_enabled);
    const _v620LocalOrK = (d.v620Flags && typeof d.v620Flags.local_or_break_k === "number")
      ? d.v620Flags.local_or_break_k : 0;
    if (_v620LocalOrBreak && _v620LocalOrK > 0) {
      pLwVal = pLwVal + " \u00b7 OR+" + _v620LocalOrK.toFixed(2) + "\u00d7ATR leg";
    }
    // v6.3.0 \u2014 noise-cross filter active suffix on the Local Weather
    // card. The filter sits in front of the EMA-cross exit on Sentinel B
    // (require adverse drawdown >= k\u00d7ATR before the cross can fire),
    // so the operator-facing weather card is the right place to surface
    // it alongside the existing OR-break leg suffix.
    //
    // v6.4.0 \u2014 when Alarm B is fully disabled (ALARM_B_ENABLED=false),
    // the noise-cross suffix is misleading (it implies the cross can still
    // fire). In that case suppress noise\u2265k\u00d7ATR and instead surface
    // the active Chandelier multipliers (chand 1.5/0.7) which are the new
    // primary trail-out signal once B no longer evaluates.
    const _v630NoiseCross = !!(d.v630Flags && d.v630Flags.noise_cross_filter_enabled);
    const _v630NoiseK = (d.v630Flags && typeof d.v630Flags.noise_cross_atr_k === "number")
      ? d.v630Flags.noise_cross_atr_k : 0;
    const _v640AlarmB = !!(d.v640Flags && d.v640Flags.alarm_b_enabled);
    const _v640ChandWide = (d.v640Flags && typeof d.v640Flags.chandelier_wide_mult === "number")
      ? d.v640Flags.chandelier_wide_mult : 0;
    const _v640ChandTight = (d.v640Flags && typeof d.v640Flags.chandelier_tight_mult === "number")
      ? d.v640Flags.chandelier_tight_mult : 0;
    if (_v640AlarmB && _v630NoiseCross && _v630NoiseK > 0) {
      pLwVal = pLwVal + " \u00b7 noise\u2265" + _v630NoiseK.toFixed(2) + "\u00d7ATR";
    } else if (!_v640AlarmB && _v640ChandWide > 0 && _v640ChandTight > 0) {
      pLwVal = pLwVal + " \u00b7 chand " + _v640ChandWide.toFixed(1) +
               "/" + _v640ChandTight.toFixed(1);
    } else if (_v630NoiseCross && _v630NoiseK > 0) {
      // Defensive fallback: pre-v6.4.0 deploys (no v640Flags block).
      pLwVal = pLwVal + " \u00b7 noise\u2265" + _v630NoiseK.toFixed(2) + "\u00d7ATR";
    }
    const pLwMetrics = _metricsHtml([
      ["Local 5m close", _fmtNum(wx.last_close_5m, 2)],
      ["Local 5m EMA9",  _fmtNum(wx.ema9_5m, 2)],
      ["Local last",     _fmtNum(wx.last, 2)],
      ["Local AVWAP",    _fmtNum(wx.avwap, 2)],
      ["DI+ 1m",          _fmtNum(di.di_plus_1m, 2)],
      ["DI\u2212 1m",     _fmtNum(di.di_minus_1m, 2)],
      ["Global QQQ",      _wxGlobal || null],
      ["Divergence",      _wxDiv ? "yes" : (_wxGlobal ? "no" : null)],
    ]);
    const _bhSide = (bh.side || "").toString().toUpperCase();
    const _bhConsec = (_bhSide === "LONG")
      ? bh.long_consecutive_outside
      : (_bhSide === "SHORT" ? bh.short_consecutive_outside : null);
    const p2bMetrics = _metricsHtml([
      ["Side",            _bhSide || null],
      ["OR high",         _fmtNum(bh.or_high, 2)],
      ["OR low",          _fmtNum(bh.or_low, 2)],
      ["Last two closes", Array.isArray(bh.last_two_closes)
        ? bh.last_two_closes.map((x) => _fmtNum(x, 2) || "\u2014").join(" / ")
        : null],
      ["Consec outside",  _fmtInt(_bhConsec)],
    ]);
    // v5.20.6 — when the volume gate is bypassed (VOLUME_GATE_ENABLED=false
    // → vol_gate_status="OFF"), the baseline / ratio rows are meaningless
    // until the 55-day history warms up. Render a single explanatory row
    // instead of four em-dashes.
    const p2vMetrics = (String(d.volStatus || "").toUpperCase() === "OFF")
      ? _metricsHtml([["Volume gate", "bypassed (warming)"]])
      : _metricsHtml([
          ["Current vol",  _fmtInt(vb.current_1m_vol)],
          ["Baseline 55d", _fmtNum(vb.baseline_at_minute, 0)],
          ["Ratio 55-bar", _fmtNum(vb.ratio_to_55bar_avg, 2)],
          ["Days avail",   (vb.days_available !== undefined && vb.days_available !== null)
            ? (_fmtInt(vb.days_available) + "/55")
            : null],
        ]);
    // v5.20.7 \u2014 Authority card sources Section-I permit alignment
    // (long_open / short_open / sovereign_anchor_open) and QQQ price
    // vs EMA9 / vs AVWAP from section_i_permit. Earlier wiring read
    // sip.open / sip.qqq_aligned / sip.index_aligned, none of which
    // are emitted by /api/state, so every row rendered as a dim em
    // dash. The rewired card surfaces the actual gating signals.
    //
    // v5.22.0 \u2014 side relevance: when an open position exists,
    // hide the irrelevant permit row. A LONG position cares about
    // long_open (and QQQ vs EMA9 / AVWAP, which are the long
    // alignment checks); a SHORT position cares about short_open.
    // Sov. anchor stays visible either way (it's bilaterally
    // relevant). When flat, both sides show as before.
    const _posSide = (d.pos && typeof d.pos.side === "string")
      ? d.pos.side.toUpperCase()
      : null;
    const _showLongAuth  = (_posSide === null) || (_posSide === "LONG");
    const _showShortAuth = (_posSide === null) || (_posSide === "SHORT");
    const _p3aRows = [];
    if (_showLongAuth) {
      _p3aRows.push(["Long permit", (sip && typeof sip.long_open === "boolean")
        ? (sip.long_open ? "yes" : "no")
        : null]);
    }
    if (_showShortAuth) {
      _p3aRows.push(["Short permit", (sip && typeof sip.short_open === "boolean")
        ? (sip.short_open ? "yes" : "no")
        : null]);
    }
    _p3aRows.push(["Sov. anchor", (sip && typeof sip.sovereign_anchor_open === "boolean")
      ? (sip.sovereign_anchor_open ? "yes" : "no")
      : null]);
    // QQQ alignment rows: when LONG show "above" check (long alignment);
    // when SHORT show "below" check (short alignment); when flat, show
    // the raw above/below comparison for both as before.
    if (_posSide === "LONG") {
      _p3aRows.push(["QQQ vs EMA9", (sip && typeof sip.qqq_5m_close === "number" && typeof sip.qqq_5m_ema9 === "number")
        ? (sip.qqq_5m_close > sip.qqq_5m_ema9 ? "above (ok)" : "below (fail)")
        : null]);
      _p3aRows.push(["QQQ vs AVWAP", (sip && typeof sip.qqq_current_price === "number" && typeof sip.qqq_avwap_0930 === "number")
        ? (sip.qqq_current_price > sip.qqq_avwap_0930 ? "above (ok)" : "below (fail)")
        : null]);
    } else if (_posSide === "SHORT") {
      _p3aRows.push(["QQQ vs EMA9", (sip && typeof sip.qqq_5m_close === "number" && typeof sip.qqq_5m_ema9 === "number")
        ? (sip.qqq_5m_close < sip.qqq_5m_ema9 ? "below (ok)" : "above (fail)")
        : null]);
      _p3aRows.push(["QQQ vs AVWAP", (sip && typeof sip.qqq_current_price === "number" && typeof sip.qqq_avwap_0930 === "number")
        ? (sip.qqq_current_price < sip.qqq_avwap_0930 ? "below (ok)" : "above (fail)")
        : null]);
    } else {
      _p3aRows.push(["QQQ vs EMA9", (sip && typeof sip.qqq_5m_close === "number" && typeof sip.qqq_5m_ema9 === "number")
        ? (sip.qqq_5m_close > sip.qqq_5m_ema9 ? "above" : "below")
        : null]);
      _p3aRows.push(["QQQ vs AVWAP", (sip && typeof sip.qqq_current_price === "number" && typeof sip.qqq_avwap_0930 === "number")
        ? (sip.qqq_current_price > sip.qqq_avwap_0930 ? "above" : "below")
        : null]);
    }
    // v6.11.0 -- C25 SPY regime rows (defensive: degrade to null if backend older).
    const spyReg = (d && d.spy_regime_today) ? d.spy_regime_today : {};
    _p3aRows.push(["SPY 9:30",    _fmtNum(spyReg.spy_open_930, 2)]);
    _p3aRows.push(["SPY 10:00",   _fmtNum(spyReg.spy_close_1000, 2)]);
    _p3aRows.push(["SPY 30m %",   (spyReg.ret_pct != null) ? (spyReg.ret_pct >= 0 ? "+" : "") + spyReg.ret_pct.toFixed(2) + "%" : null]);
    _p3aRows.push(["SPY regime",  spyReg.regime || null]);
    _p3aRows.push(["C25 amp",     (d && d.v611_regime_b_active) ? "ACTIVE (1.5x short)" : "idle"]);
    const p3aMetrics = _metricsHtml(_p3aRows);

    // v5.22.0 \u2014 Momentum card: when in position, only show the
    // side that matters. LONG cares about DI+ (long-side momentum);
    // SHORT cares about DI- (short-side momentum). Threshold and seed
    // are always shown.
    const _p3mRows = [];
    if (_posSide === "LONG") {
      _p3mRows.push(["DI+ 1m", _fmtNum(di.di_plus_1m, 2)]);
      _p3mRows.push(["DI+ 5m", _fmtNum(di.di_plus_5m, 2)]);
    } else if (_posSide === "SHORT") {
      _p3mRows.push(["DI- 1m", _fmtNum(di.di_minus_1m, 2)]);
      _p3mRows.push(["DI- 5m", _fmtNum(di.di_minus_5m, 2)]);
    } else {
      _p3mRows.push(["DI+ 1m", _fmtNum(di.di_plus_1m, 2)]);
      _p3mRows.push(["DI- 1m", _fmtNum(di.di_minus_1m, 2)]);
      _p3mRows.push(["DI+ 5m", _fmtNum(di.di_plus_5m, 2)]);
      _p3mRows.push(["DI- 5m", _fmtNum(di.di_minus_5m, 2)]);
    }
    _p3mRows.push(["Threshold", _fmtNum(di.threshold, 2)]);
    _p3mRows.push(["Seed bars", (di.seed_bars !== undefined && di.seed_bars !== null)
      ? (_fmtInt(di.seed_bars)
          + (typeof di.sufficient === "boolean" ? (di.sufficient ? " (ok)" : " (low)") : ""))
      : null]);
    // v6.0.0 \u2014 distance-to-next-trigger insights. Surfaces how far
    // each Phase 3 gate is from flipping so an operator can scan for
    // "about to fire" tickers without reading raw indicator feeds.
    // ADX 5m gap: positive = below the 20 trigger; <=0 = passing.
    // DI long/short gap: threshold \u2212 DI; positive = below threshold.
    // DI cross: DI+ \u2212 DI-; positive = long-leaning. VWAP/EMA9 gaps
    // are signed % deltas of last vs the level. Side-aware in-position.
    const md = (d.ptv510 && d.ptv510.momentum_distances) || {};
    function _fmtGap(v, digits, suffix) {
      const s = _fmtNum(v, digits);
      if (s === null) return null;
      const n = Number(v);
      const sign = (isFinite(n) && n > 0) ? "+" : "";
      return sign + s + (suffix || "");
    }
    if (typeof md.adx_5m === "number" || typeof md.adx_5m_gap === "number") {
      const adxLabel = (typeof md.adx_5m === "number")
        ? _fmtNum(md.adx_5m, 2) + " (gap " + _fmtGap(md.adx_5m_gap, 2) + ")"
        : ("gap " + _fmtGap(md.adx_5m_gap, 2));
      _p3mRows.push(["ADX 5m", adxLabel]);
    }
    if (_posSide === "LONG") {
      _p3mRows.push(["DI+ gap", _fmtGap(md.di_long_gap, 2)]);
    } else if (_posSide === "SHORT") {
      _p3mRows.push(["DI- gap", _fmtGap(md.di_short_gap, 2)]);
    } else {
      _p3mRows.push(["DI+ gap", _fmtGap(md.di_long_gap, 2)]);
      _p3mRows.push(["DI- gap", _fmtGap(md.di_short_gap, 2)]);
    }
    _p3mRows.push(["DI cross", _fmtGap(md.di_cross_gap, 2)]);
    _p3mRows.push(["vs AVWAP",  _fmtGap(md.vwap_gap_pct, 3, "%")]);
    _p3mRows.push(["vs EMA9 5m", _fmtGap(md.ema9_gap_pct, 3, "%")]);
    const p3mMetrics = _metricsHtml(_p3mRows);
    // v5.20.7 \u2014 the per-position cards (Sovereign brake / Velocity
    // fuse / Strikes) are only meaningful while a position is open. With
    // no open position the upstream wiring delivers ppv510=null, which
    // becomes ppv={} here and every row renders as a dim em dash. When
    // that happens, surface a single explanatory row (same UX as the
    // volume-bypass treatment in v5.20.6).
    const _hasOpenPos = !!(ppv && Object.keys(ppv).length > 0);
    const alAMetrics = _hasOpenPos
      ? _metricsHtml([
          ["Unrealized",       _fmtPct(sb.unrealized_pct, 2)],
          ["Loss threshold",   _fmtPct(sb.brake_threshold_pct, 2)],
          ["Time in pos",      (sb.time_in_position_min !== undefined && sb.time_in_position_min !== null)
            ? (_fmtNum(sb.time_in_position_min, 1) + " min")
            : null],
        ])
      : _metricsHtml([["Status", "(no open position)"]]);
    const alBMetrics = _hasOpenPos
      ? _metricsHtml([
          ["1-min velocity",   _fmtPct(vf.last_5m_move_pct, 3)],
          ["Flash threshold",  _fmtPct(vf.fuse_threshold_pct, 2)],
        ])
      : _metricsHtml([["Status", "(no open position)"]]);
    const stkHistory = Array.isArray(stk.strike_history) ? stk.strike_history : [];
    const _stkLast5 = stkHistory.slice(-5).map((e) => {
      if (!e) return "\u2014";
      if (typeof e === "string") return e;
      const t = e.ts || e.time || "";
      const k = e.kind || e.event || "";
      return (t + (k ? (" " + k) : "")).trim() || "\u2014";
    });
    const posMetrics = _hasOpenPos
      ? _metricsHtml([
          ["Strikes used",  (stk.strikes_count !== undefined && stk.strikes_count !== null)
            ? (_fmtInt(stk.strikes_count) + "/3")
            : null],
          ["History",       _stkLast5.length ? _stkLast5.join(" \u00b7 ") : null],
        ])
      : _metricsHtml([["Status", "(no open position)"]]);

    function card(chip, name, desc, state, val, metrics) {
      return '<div class="pmtx-comp-card pmtx-comp-' + state + '">'
        +   '<div class="pmtx-comp-head">'
        +     '<span class="pmtx-comp-chip">' + esc(chip) + '</span>'
        +     '<span class="pmtx-comp-name">' + escapeHtml(name) + '</span>'
        +   '</div>'
        +   '<div class="pmtx-comp-desc">' + escapeHtml(desc) + '</div>'
        +   '<div class="pmtx-comp-state">'
        +     '<span class="pmtx-comp-badge">' + escapeHtml(state.toUpperCase()) + '</span>'
        +     '<span class="pmtx-comp-val">' + escapeHtml(val) + '</span>'
        +   '</div>'
        +   (metrics || "")
        + '</div>';
    }

    // v5.29.0 — Volume card hidden when feature flag bypasses the gate.
    // The Volume column in the matrix table hides at the same time so the
    // expanded view stays consistent with the row above. When d.showVolume
    // is undefined (legacy callers), the card renders — preserves prior
    // behaviour for any embedder that hasn't been updated yet.
    const _showVolume = (d.showVolume !== false);
    return '<div class="pmtx-comp-grid" data-pmtx-comp-grid="v5.23.0">'
      +   '<div class="pmtx-comp-head-line">Pipeline components \u00b7 live state</div>'
      +   '<div class="pmtx-comp-cards">'
      +     card("P1", "Weather",     "QQQ regime + AVWAP",        p1State,  p1Val,  p1Metrics)
      +     card("P1", "Local Weather", "Per-stock EMA9 + AVWAP + DI", pLwState, pLwVal, pLwMetrics)
      +     card("P2", "Boundary",    "Two consec 1m closes thru OR", p2bState, p2bVal, p2bMetrics)
      +     (_showVolume ? card("P2", "Volume", "1m vol \u2265 100% of 55-bar avg", p2vState, p2vVal, p2vMetrics) : '')
      +     card("P3", "Authority",   "Permit & QQQ alignment",    p3aState, p3aVal, p3aMetrics)
      +     card("P3", "Momentum",    "5m ADX > 20",                  p3mState, p3mVal, p3mMetrics)
      +     card("AL", "A1 Loss",     "Per-position $ stop",          alAState, alAVal, alAMetrics)
      +     card("AL", "A2 Flash",    "1-min adverse velocity %",    alBState, alBVal, alBMetrics)
      +     card("POS", "Strikes",    "Strikes used today (cap 3)",   posState, posVal, posMetrics)
      +   '</div>'
      + '</div>';
  }

  // Build per-ticker index lookups so the matrix renders in O(N).
  // phase3 + phase4 can have BOTH long and short rows for the same
  // ticker, so we key the primary lookup by "TICKER:SIDE" and also
  // keep a fallback by bare ticker (whichever side appears first).
  function _pmtxIndex(ts) {
    const idx = { p2: {}, p3: {}, p4: {} };
    if (ts && Array.isArray(ts.phase2)) {
      ts.phase2.forEach((r) => { if (r && r.ticker) idx.p2[r.ticker] = r; });
    }
    if (ts && Array.isArray(ts.phase3)) {
      ts.phase3.forEach((r) => {
        if (!r || !r.ticker) return;
        const k = r.ticker + ":" + (r.side || "LONG");
        idx.p3[k] = r;
        if (!idx.p3[r.ticker]) idx.p3[r.ticker] = r;
      });
    }
    if (ts && Array.isArray(ts.phase4)) {
      ts.phase4.forEach((r) => {
        if (!r || !r.ticker) return;
        const k = r.ticker + ":" + (r.side || "LONG");
        idx.p4[k] = r;
        if (!idx.p4[r.ticker]) idx.p4[r.ticker] = r;
      });
    }
    return idx;
  }

  // v5.18.1 \u2014 dual-scope element lookup so renderWeatherCheck and
  // renderPermitMatrix can render into either Main's id-bearing DOM or
  // a per-panel skeleton (Val/Gene) that uses [data-f="..."] hooks.
  // When `panel` is null/undefined we fall back to document-level
  // getElementById; otherwise we look up by data-f attribute within the
  // panel root. Same key string is used in both branches \u2014 callers
  // just pass the bare key (e.g. "pmtx-weather").
  function _pmtxEl(panel, key) {
    if (panel) return panel.querySelector('[data-f="' + key + '"]');
    return $(key);
  }

  // \u2500\u2500\u2500 Weather Check banner (Phase 1 verdict) \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
  // Reads tiger_sovereign.phase1.{long,short}.{permit,qqq_5m_close,
  // qqq_5m_ema9,qqq_avwap_0930,qqq_last}. Sets one of the four
  // .pmtx-weather-{green,red,amber,pending} state classes.
  // v5.18.1 \u2014 optional `panel` arg lets Val/Gene tabs reuse the
  // exact same renderer against their own copy of the DOM.
  // v7.58.0 -- renderWeatherCheck removed (vestigial: card HTML deleted in this PR).
  // exact same renderer (data is market-wide, just different DOM mount).
  // v7.58.0 -- renderPermitMatrix removed (vestigial: card HTML deleted in this PR).
  // a string suitable for direct concat into the detail HTML.
  function _pmtxIntradayChartPanel(tkr) {
    if (!tkr) return "";
    return '<div class="pmtx-intraday-section" data-intraday-chart="' + escapeHtml(tkr) + '">'
      +   '<div class="pmtx-intraday-head">'
      +     '<span class="pmtx-intraday-title">Intraday \u00b7 ' + escapeHtml(tkr) + '</span>'
      +     '<span class="pmtx-intraday-meta" data-intraday-meta>Loading bars\u2026</span>'
      +   '</div>'
      +   '<canvas class="pmtx-intraday-canvas" data-intraday-canvas width="1200" height="320"></canvas>'
      +   '<div class="pmtx-intraday-legend">'
      +     '<span class="pmtx-intraday-leg pmtx-intraday-leg-or">OR H/L</span>'
      +     '<span class="pmtx-intraday-leg pmtx-intraday-leg-vol">Volume</span>'
      +     '<span class="pmtx-intraday-leg pmtx-intraday-leg-entry">Entry</span>'
      +     '<span class="pmtx-intraday-leg pmtx-intraday-leg-exit">Exit</span>'
      +     '<span class="pmtx-intraday-leg pmtx-intraday-leg-stop">Stop</span>'
      +     '<span class="pmtx-intraday-leg pmtx-intraday-leg-be">1R (move-to-BE)</span>'
      +     '<span class="pmtx-intraday-leg pmtx-intraday-leg-target">+2.5R target</span>'
      +     '<span class="pmtx-intraday-hint" title="Wheel zooms, drag pans, hover for OHLC tooltip, double-click resets the view">scroll \u00b7 drag \u00b7 dblclick</span>'
      +   '</div>'
      + '</div>';
  }

  // Cache so we don't refetch on every state poll. Keyed by ticker;
  // value is { ts, payload } where ts is monotonic ms. TTL 60s \u2014
  // long enough to cover several /api/state cycles, short enough that
  // a freshly-printed bar shows up within a minute.
  const _intradayCache = {};
  const _INTRADAY_TTL_MS = 60 * 1000;

  // v6.0.0 \u2014 per-canvas chart-view state for zoom/pan/hover. The
  // canvas DOM node is the key; we attach a state object so wheel/drag
  // event listeners can mutate the visible window and trigger redraws
  // without recomputing the payload. Defaults to the full 7am\u20135pm CT
  // window (et_min 480\u20131080).
  //
  // v6.0.1 \u2014 the matrix re-renders on every /api/state poll, which
  // tears down the canvas DOM node. The WeakMap-keyed-by-canvas store
  // therefore lost its zoom/pan window each render, snapping the chart
  // back to the full session within ~1s. Fix: persist the user-visible
  // window per ticker in a plain dict that survives canvas destruction,
  // and seed each freshly-mounted canvas from it. Hover/wired stay
  // per-canvas (transient UI state).
  // v9.1.10 -- separate "RTH default" from "full max-extent" so the
  // default view stays RTH-only (operator request) while pan/zoom
  // out to pre/post-market is still possible. The v9.1.9 single
  // _CHART_FULL_X_MIN/MAX = 570/960 was a regression because the
  // wheel/drag clamps used the same constants -- so zoom-out was
  // effectively disabled.
  //
  //   _CHART_RTH_X_MIN/MAX   -> default view on first load
  //   _CHART_FULL_X_MIN/MAX  -> max extent the user can pan/zoom to
  const _CHART_RTH_X_MIN = 570;   // 09:30 ET
  const _CHART_RTH_X_MAX = 960;   // 16:00 ET
  const _CHART_FULL_X_MIN = 240;  // 04:00 ET
  const _CHART_FULL_X_MAX = 1200; // 20:00 ET
  const _chartViewState = new WeakMap();
  const _chartViewByTkr = {};
  function _chartTkrKey(canvas) {
    return (canvas && canvas.dataset && canvas.dataset.intradayTkr) || "";
  }
  function _chartGetState(canvas) {
    let st = _chartViewState.get(canvas);
    if (!st) {
      const tkr = _chartTkrKey(canvas);
      const persisted = tkr && _chartViewByTkr[tkr];
      st = {
        // v9.1.10 -- default to RTH view; persisted state (set by the
        // user's pan/zoom) overrides.
        xMin: persisted ? persisted.xMin : _CHART_RTH_X_MIN,
        xMax: persisted ? persisted.xMax : _CHART_RTH_X_MAX,
        hoverEtMin: null,
        hoverPx: null,
        hoverPy: null,
        wired: false,
      };
      _chartViewState.set(canvas, st);
    }
    return st;
  }
  function _chartPersistView(canvas, st) {
    const tkr = _chartTkrKey(canvas);
    if (!tkr) return;
    _chartViewByTkr[tkr] = { xMin: st.xMin, xMax: st.xMax };
  }

  function _isMobile() {
    try {
      return window.matchMedia && window.matchMedia("(max-width: 720px)").matches;
    } catch (e) {
      return false;
    }
  }

  // Resample 1m bars to 5m for the mobile path. Aligns to wall-clock
  // 5-minute boundaries via et_min so 09:30, 09:35 \u2026 are stable.
  function _resample5m(bars) {
    const out = [];
    let cur = null;
    for (let i = 0; i < bars.length; i++) {
      const b = bars[i];
      if (typeof b.et_min !== "number") continue;
      const bucket = Math.floor(b.et_min / 5) * 5;
      if (!cur || cur.et_min !== bucket) {
        if (cur) out.push(cur);
        cur = { et_min: bucket, ts: b.ts, o: b.o, h: b.h, l: b.l, c: b.c, v: b.v || 0,
                avwap: b.avwap, ema9_5m: b.ema9_5m };
      } else {
        cur.h = Math.max(cur.h, b.h);
        cur.l = Math.min(cur.l, b.l);
        cur.c = b.c;
        cur.v = (cur.v || 0) + (b.v || 0);
        if (b.avwap !== null && b.avwap !== undefined) cur.avwap = b.avwap;
        if (b.ema9_5m !== null && b.ema9_5m !== undefined) cur.ema9_5m = b.ema9_5m;
      }
    }
    if (cur) out.push(cur);
    return out;
  }

  function _drawIntradayChart(canvas, payload) {
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    // Resize to backing-store pixels for crisp rendering on HiDPI.
    const dpr = window.devicePixelRatio || 1;
    const cssW = canvas.clientWidth || 1200;
    const cssH = canvas.clientHeight || 320;
    if (canvas.width !== Math.round(cssW * dpr)) canvas.width = Math.round(cssW * dpr);
    if (canvas.height !== Math.round(cssH * dpr)) canvas.height = Math.round(cssH * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);

    const rawBars = payload && Array.isArray(payload.bars) ? payload.bars : [];
    if (!rawBars.length) {
      ctx.fillStyle = "#888";
      ctx.font = "13px system-ui, sans-serif";
      ctx.fillText("No bars yet for today\u2014waiting for the WS feed.", 12, 28);
      return;
    }
    const bars = _isMobile() ? _resample5m(rawBars) : rawBars;

    // v6.11.8 — X axis spans 4:00 ET (=3am CT, et_min=240) to 20:00 ET
    // (=7pm CT, et_min=1200). Covers the FULL US equities pre-market
    // (04:00–09:30 ET) and post-market (16:00–20:00 ET) windows so
    // overnight / extended-hours moves are visible on the dashboard.
    // (v5.23.3 used 480/1080 = 8am ET / 18:00 ET = late-premarket only.)
    // v6.0.0 — X window is per-canvas state so wheel/drag pan-zoom works.
    const _vs = _chartGetState(canvas);
    // v9.1.10 -- default to RTH-only (570/960) on first paint; the
    // _vs persisted-by-user values can extend out to the full
    // 240/1200 envelope via wheel/drag. The downstream clamps below
    // floor at 240/1200 so the user can't pan past the full window.
    let X_MIN = (typeof _vs.xMin === "number") ? _vs.xMin : _CHART_RTH_X_MIN;
    let X_MAX = (typeof _vs.xMax === "number") ? _vs.xMax : _CHART_RTH_X_MAX;
    if (X_MAX - X_MIN < 30) X_MAX = X_MIN + 30; // floor at 30 min
    if (X_MIN < _CHART_FULL_X_MIN) X_MIN = _CHART_FULL_X_MIN;
    if (X_MAX > _CHART_FULL_X_MAX) X_MAX = _CHART_FULL_X_MAX;
    _vs.xMin = X_MIN; _vs.xMax = X_MAX;
    // Y axis: tight envelope around prices + OR levels (visible window).
    // v6.0.0 — Y is now scoped to bars within [X_MIN, X_MAX] so zoomed
    // views adjust their price range automatically.
    let yMin = Infinity, yMax = -Infinity;
    for (const b of bars) {
      if (typeof b.et_min === "number" && (b.et_min < X_MIN || b.et_min > X_MAX)) continue;
      if (typeof b.l === "number") yMin = Math.min(yMin, b.l);
      if (typeof b.h === "number") yMax = Math.max(yMax, b.h);
    }
    // v6.11.9 — Only widen Y to OR high/low once the OR window has closed
    // (>= 09:35 ET). Before that, payload.or_high/low can carry stale
    // values from the previous session, which forces premarket candles
    // into a sliver at the bottom of the plot (overflow into X-axis
    // labels). The server stamps `or_fresh: true` when m.or_collected_date
    // matches today (ET); fall back to false-on-missing for safety so
    // an older server build never widens Y to stale OR levels.
    const oh = (typeof payload.or_high === "number") ? payload.or_high : null;
    const ol = (typeof payload.or_low === "number") ? payload.or_low : null;
    const _orFresh = !!payload.or_fresh;
    if (_orFresh) {
      if (oh !== null) { yMin = Math.min(yMin, oh); yMax = Math.max(yMax, oh); }
      if (ol !== null) { yMin = Math.min(yMin, ol); yMax = Math.max(yMax, ol); }
    }
    if (!isFinite(yMin) || !isFinite(yMax)) return;
    const yPad = (yMax - yMin) * 0.08 || 0.5;
    yMin -= yPad; yMax += yPad;

    const PAD_L = 56, PAD_R = 12, PAD_T = 14, PAD_B = 22;
    const plotW = cssW - PAD_L - PAD_R;
    const plotH = cssH - PAD_T - PAD_B;
    // v5.31.0 \u2014 split plot vertically: price 85% on top, volume 15% on bottom.
    const VOL_FRAC = 0.15;
    const priceH = Math.max(40, plotH * (1 - VOL_FRAC) - 4);
    const volH = Math.max(20, plotH * VOL_FRAC);
    const VOL_TOP = PAD_T + priceH + 4;
    const xOf = (m) => PAD_L + ((m - X_MIN) / (X_MAX - X_MIN)) * plotW;
    const yOf = (p) => PAD_T + (1 - (p - yMin) / (yMax - yMin)) * priceH;

    // Background + grid + 9:30 ET vertical separator.
    ctx.fillStyle = "#0e1318";
    ctx.fillRect(PAD_L, PAD_T, plotW, priceH);
    ctx.fillStyle = "#0b1014";
    ctx.fillRect(PAD_L, VOL_TOP, plotW, volH);
    ctx.strokeStyle = "#1f2730";
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let i = 1; i < 6; i++) {
      const y = PAD_T + (i / 6) * priceH;
      ctx.moveTo(PAD_L, y); ctx.lineTo(PAD_L + plotW, y);
    }
    ctx.stroke();
    // v6.11.8 — vertical separators at RTH open (9:30 ET=570) and
    // RTH close (16:00 ET=960). Premarket sits left of 570, postmarket
    // sits right of 960. Only drawn when the window includes them.
    ctx.strokeStyle = "#3a4a5c";
    ctx.beginPath();
    if (570 >= X_MIN && 570 <= X_MAX) {
      ctx.moveTo(xOf(570), PAD_T); ctx.lineTo(xOf(570), PAD_T + priceH);
    }
    if (960 >= X_MIN && 960 <= X_MAX) {
      ctx.moveTo(xOf(960), PAD_T); ctx.lineTo(xOf(960), PAD_T + priceH);
    }
    ctx.stroke();

    // Y axis labels (5 ticks).
    ctx.fillStyle = "#9aa6b2";
    ctx.font = "11px system-ui, sans-serif";
    ctx.textAlign = "right";
    for (let i = 0; i <= 4; i++) {
      const p = yMin + (i / 4) * (yMax - yMin);
      const y = yOf(p);
      ctx.fillText("$" + p.toFixed(2), PAD_L - 6, y + 4);
    }
    // v6.11.8 — X axis labels in CT (operator's local TZ) for the full
    // 4am ET–8pm ET window. Mapping ET→CT is -1h: 4am ET=3am CT,
    // 8am ET=7am CT, 9:30 ET=8:30 CT (RTH open separator), 13:00 ET=12 CT,
    // 16:00 ET=3pm CT (RTH close), 20:00 ET=7pm CT.
    // v6.11.9 — align edge ticks (3am at left edge, 7pm at right edge)
    // to their respective borders so they don't get clipped on narrow
    // mobile canvases. Interior ticks stay centered.
    const xTicks = [
      {m: 240,  l: "3am",  align: "left"},
      {m: 480,  l: "7am",  align: "center"},
      {m: 570,  l: "8:30", align: "center"},
      {m: 780,  l: "12pm", align: "center"},
      {m: 960,  l: "3pm",  align: "center"},
      {m: 1200, l: "7pm",  align: "right"},
    ];
    for (const t of xTicks) {
      ctx.textAlign = t.align;
      ctx.fillText(t.l, xOf(t.m), cssH - 6);
    }
    ctx.textAlign = "center";

    // OR high/low horizontal lines.
    // v6.11.9 — only draw if `_orFresh` (post 09:35 ET). Pre-RTH OR
    // values can be carry-overs from the previous session and would
    // render miles above today's price action.
    ctx.lineWidth = 1.5;
    if (_orFresh && oh !== null) {
      ctx.strokeStyle = "#e5b85c";
      ctx.setLineDash([4, 3]);
      ctx.beginPath();
      ctx.moveTo(PAD_L, yOf(oh)); ctx.lineTo(PAD_L + plotW, yOf(oh));
      ctx.stroke();
    }
    if (_orFresh && ol !== null) {
      ctx.strokeStyle = "#e5b85c";
      ctx.setLineDash([4, 3]);
      ctx.beginPath();
      ctx.moveTo(PAD_L, yOf(ol)); ctx.lineTo(PAD_L + plotW, yOf(ol));
      ctx.stroke();
    }
    ctx.setLineDash([]);

    // v7.61.0 -- PDC, sess HOD/LOD, AVWAP \u00b11\u03c3 band, AVWAP line, PM
    // AVWAP, and EMA9(5m) overlays removed. None are part of the v10
    // ORB decision path (they were Tiger Sovereign-era inputs). The
    // intraday endpoint still returns the fields; the chart just
    // doesn't render them anymore.

    // v9.1.10 -- line chart through closes (operator request: clearer
    // intraday trajectory than the OHLC candles, which were dense and
    // visually noisy especially zoomed-out). The line is colored by
    // the cumulative direction (first-close vs last-close) so an
    // operator gets a quick green/red read without parsing candles.
    // Bar-level OHLC is still in the payload (via _intradayCache) for
    // any future hover-tooltip detail.
    var _firstVisibleClose = null, _lastVisibleClose = null;
    for (const b of bars) {
      if (typeof b.et_min !== "number") continue;
      if (b.et_min < X_MIN || b.et_min > X_MAX) continue;
      if (typeof b.c !== "number") continue;
      if (_firstVisibleClose === null) _firstVisibleClose = b.c;
      _lastVisibleClose = b.c;
    }
    const _lineUp = (_lastVisibleClose != null && _firstVisibleClose != null)
                      ? (_lastVisibleClose >= _firstVisibleClose) : true;
    ctx.strokeStyle = _lineUp ? "#3ec28f" : "#e26a6a";
    ctx.fillStyle = ctx.strokeStyle;
    ctx.lineWidth = 1.5;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    ctx.beginPath();
    let _pathStarted = false;
    for (const b of bars) {
      if (typeof b.et_min !== "number") continue;
      if (typeof b.c !== "number") continue;
      const x = xOf(b.et_min);
      const yC = yOf(b.c);
      if (!_pathStarted) { ctx.moveTo(x, yC); _pathStarted = true; }
      else { ctx.lineTo(x, yC); }
    }
    if (_pathStarted) ctx.stroke();

    // v5.31.0 \u2014 Volume sub-pane histogram (slate bars, scaled to max v).
    // v9.1.10 -- bar width is now computed locally (was shared with the
    // candle loop pre-line-chart switch).
    {
      const bw = Math.max(1, Math.min(6, plotW / Math.max(bars.length, 1) - 1));
      let vMax = 0;
      for (const b of bars) {
        if (typeof b.v === "number" && b.v > vMax) vMax = b.v;
      }
      if (vMax > 0) {
        ctx.fillStyle = "#334155";
        for (const b of bars) {
          if (typeof b.et_min !== "number") continue;
          if (typeof b.v !== "number" || b.v <= 0) continue;
          const x = xOf(b.et_min);
          const h = (b.v / vMax) * (volH - 2);
          ctx.fillRect(x - bw / 2, VOL_TOP + (volH - h), bw, h);
        }
      }
    }

    // v7.61.0 -- AVWAP / PM AVWAP / EMA9(5m) draws removed; not v10.

    // v7.61.0 -- v10 entry overlays: for each long/short admit today,
    // draw the stop / 1R move-to-BE / 2.5R target as horizontal
    // reference lines extending from the entry timestamp rightward.
    // Stop is sourced from the OR band (LONG uses or_low, SHORT uses
    // or_high) since v10's stop = opposite-side OR + buffer. Target
    // is RR=2.5 from the v10 keystone (cfg.rr is 2.5).
    if (_orFresh && oh !== null && ol !== null) {
      const tradesForOverlay = (payload && Array.isArray(payload.trades)) ? payload.trades : [];
      const v10rr = 2.5;
      for (const t of tradesForOverlay) {
        if (!t || typeof t.entry_price !== "number" || !t.entry_ts) continue;
        const entryEtMin = utcIsoToEtMin(t.entry_ts);
        if (entryEtMin === null) continue;
        const side = (t.side || "").toString().toLowerCase();
        const isLong = side !== "short";
        const stopPx = isLong ? ol : oh;
        const risk = Math.abs(t.entry_price - stopPx);
        if (risk <= 0) continue;
        const oneR = isLong ? t.entry_price + risk : t.entry_price - risk;
        const target = isLong ? t.entry_price + v10rr * risk
                              : t.entry_price - v10rr * risk;
        // X range: from entry timestamp to exit timestamp (if exited)
        // or to the right edge of the plot (if still open).
        const exitEtMin = t.exit_ts ? utcIsoToEtMin(t.exit_ts) : null;
        const x1 = xOf(Math.max(X_MIN, entryEtMin));
        const x2 = (exitEtMin !== null && exitEtMin <= X_MAX)
          ? xOf(Math.min(X_MAX, exitEtMin))
          : (PAD_L + plotW);
        function hline(price, color, label, dash) {
          if (price < yMin || price > yMax) return;
          ctx.strokeStyle = color;
          ctx.lineWidth = 1.2;
          if (dash) ctx.setLineDash(dash); else ctx.setLineDash([]);
          ctx.beginPath();
          ctx.moveTo(x1, yOf(price));
          ctx.lineTo(x2, yOf(price));
          ctx.stroke();
          ctx.setLineDash([]);
          ctx.fillStyle = color;
          ctx.font = "10px system-ui, sans-serif";
          ctx.textAlign = "left";
          ctx.fillText(label, x1 + 4, yOf(price) - 2);
        }
        // Stop -- dashed red.
        hline(stopPx, "#ef4444", "stop", [5, 4]);
        // 1R move-to-BE marker -- thin amber.
        hline(oneR, "#fbbf24", "1R", [3, 3]);
        // Target (2.5R) -- dashed green.
        hline(target, "#22c55e", "+2.5R target", [5, 4]);
      }
    }

    // v5.23.3 \u2014 ET minute-of-day mapper (DST-safe via Intl). v5.31.0
    // hoisted above sentinel/lifecycle markers so they can share it.
    const utcIsoToEtMin = (tsIso) => {
      if (!tsIso || typeof tsIso !== "string") return null;
      const dt = new Date(tsIso);
      if (isNaN(dt.getTime())) return null;
      // Use Intl to get ET hour/minute; DST-safe.
      try {
        const parts = new Intl.DateTimeFormat("en-US", {
          timeZone: "America/New_York",
          hour12: false,
          hour: "2-digit",
          minute: "2-digit",
        }).formatToParts(dt);
        let hh = 0, mm = 0;
        for (const p of parts) {
          if (p.type === "hour") hh = parseInt(p.value, 10);
          if (p.type === "minute") mm = parseInt(p.value, 10);
        }
        if (hh === 24) hh = 0;  // Intl quirk
        return hh * 60 + mm;
      } catch (_e) {
        return null;
      }
    };

    // v7.61.0 -- Sentinel arm/trip markers removed. Sentinel was a
    // Tiger Sovereign Phase 4 instrumentation; v10 ORB doesn't emit
    // these events. The intraday payload still carries
    // sentinel_events (legacy field) but we no longer render them.

    // v5.23.3 \u2014 Entry/exit markers, sourced from paper_state (open
    // positions + closed history). Each ts is full ISO UTC; we map
    // it to ET minute-of-day directly so the marker aligns with the
    // chart's ET-anchored x-axis instead of using rough hour-arithmetic.
    const trades = (payload && Array.isArray(payload.trades)) ? payload.trades : [];
    for (const t of trades) {
      const drawMark = (tsIso, price, kind) => {
        if (!tsIso || typeof price !== "number") return;
        const etMin = utcIsoToEtMin(tsIso);
        if (etMin === null) return;
        // Clamp to plot window so off-axis trades don't draw outside.
        if (etMin < X_MIN || etMin > X_MAX) return;
        const x = xOf(etMin);
        const y = yOf(price);
        ctx.fillStyle = kind === "entry" ? "#3ec28f" : "#e26a6a";
        ctx.beginPath();
        if (kind === "entry") {
          // upward triangle
          ctx.moveTo(x, y - 6); ctx.lineTo(x - 5, y + 4); ctx.lineTo(x + 5, y + 4);
        } else {
          // downward triangle (X-mark style for exits would also work)
          ctx.moveTo(x, y + 6); ctx.lineTo(x - 5, y - 4); ctx.lineTo(x + 5, y - 4);
        }
        ctx.closePath();
        ctx.fill();
      };
      drawMark(t.entry_ts, t.entry_price, "entry");
      drawMark(t.exit_ts, t.exit_price, "exit");
    }

    // v5.31.0 \u2014 Lifecycle overlay (entries, exits, trail-stop staircase,
    // MAE/MFE band, live position rail). Source: payload.lifecycle, populated
    // by dashboard_server._intraday_build_lifecycle from forensic streams.
    _drawLifecycleOverlay(ctx, payload, {
      xOf: xOf, yOf: yOf,
      X_MIN: X_MIN, X_MAX: X_MAX,
      utcIsoToEtMin: utcIsoToEtMin,
      yMin: yMin, yMax: yMax,
      PAD_L: PAD_L, plotW: plotW,
    });

    // v6.0.0 \u2014 hover crosshair + tooltip. The pointer-handler stores
    // hoverEtMin/hoverPx/hoverPy on the canvas state and redraws.
    if (typeof _vs.hoverEtMin === "number"
        && _vs.hoverEtMin >= X_MIN && _vs.hoverEtMin <= X_MAX) {
      const hx = xOf(_vs.hoverEtMin);
      ctx.strokeStyle = "rgba(180,200,220,0.35)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(hx, PAD_T); ctx.lineTo(hx, PAD_T + priceH);
      ctx.stroke();
      // Find nearest bar by et_min for tooltip.
      let near = null;
      let bestD = Infinity;
      for (let i = 0; i < bars.length; i++) {
        const b = bars[i];
        if (typeof b.et_min !== "number") continue;
        const d = Math.abs(b.et_min - _vs.hoverEtMin);
        if (d < bestD) { bestD = d; near = b; }
      }
      if (near) {
        const lines = [];
        // Convert ET min to CT label (HH:MM).
        const _etH = Math.floor(near.et_min / 60);
        const _etM = near.et_min % 60;
        let _ctH = _etH - 1; if (_ctH < 0) _ctH += 24;
        const tlabel = String(_ctH).padStart(2, "0") + ":" + String(_etM).padStart(2, "0") + " CT";
        lines.push(tlabel);
        if (typeof near.o === "number") lines.push("O " + near.o.toFixed(2));
        if (typeof near.h === "number") lines.push("H " + near.h.toFixed(2));
        if (typeof near.l === "number") lines.push("L " + near.l.toFixed(2));
        if (typeof near.c === "number") lines.push("C " + near.c.toFixed(2));
        if (typeof near.v === "number") lines.push("V " + (near.v >= 1000 ? (near.v/1000).toFixed(1)+"k" : String(near.v)));
        if (typeof near.avwap === "number") lines.push("AVWAP " + near.avwap.toFixed(2));
        if (typeof near.ema9_5m === "number") lines.push("EMA9 " + near.ema9_5m.toFixed(2));
        // Draw tooltip box.
        ctx.font = "11px system-ui, sans-serif";
        const lineH = 13;
        let boxW = 0;
        for (const l of lines) boxW = Math.max(boxW, ctx.measureText(l).width);
        boxW += 12;
        const boxH = lineH * lines.length + 10;
        let bx = hx + 8;
        if (bx + boxW > PAD_L + plotW) bx = hx - boxW - 8;
        let by = PAD_T + 8;
        if (typeof _vs.hoverPy === "number") {
          by = Math.max(PAD_T + 4, Math.min(PAD_T + priceH - boxH - 4, _vs.hoverPy - boxH/2));
        }
        ctx.fillStyle = "rgba(14,19,24,0.92)";
        ctx.strokeStyle = "#2a3540";
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.rect(bx, by, boxW, boxH);
        ctx.fill();
        ctx.stroke();
        ctx.fillStyle = "#cdd6e0";
        ctx.textAlign = "left";
        for (let i = 0; i < lines.length; i++) {
          ctx.fillText(lines[i], bx + 6, by + 14 + i * lineH);
        }
      }
    }
  }

  // v6.0.0 \u2014 Wire pointer + wheel events on the intraday canvas to
  // drive the per-canvas chart-view state. Idempotent via _vs.wired.
  // Wheel = zoom centered on cursor; drag = pan; double-click = reset.
  function _wireIntradayChartInteraction(canvas) {
    const _vs = _chartGetState(canvas);
    if (_vs.wired) return;
    _vs.wired = true;
    const _xMinFromPx = (px, plotL, plotW) => _vs.xMin + ((px - plotL) / plotW) * (_vs.xMax - _vs.xMin);
    const _layout = () => {
      const dpr = window.devicePixelRatio || 1;
      const cssW = canvas.clientWidth || 1200;
      const cssH = canvas.clientHeight || 320;
      // Match _drawIntradayChart pads.
      const PAD_L = 56, PAD_R = 12, PAD_T = 14, PAD_B = 22;
      return { dpr, cssW, cssH, PAD_L, PAD_R, PAD_T, PAD_B,
               plotW: cssW - PAD_L - PAD_R, plotH: cssH - PAD_T - PAD_B };
    };
    const _redraw = () => {
      const cached = _intradayCache[canvas.dataset.intradayTkr || ""];
      const payload = cached ? cached.payload : canvas._lastPayload;
      if (payload) _drawIntradayChart(canvas, payload);
    };
    canvas.addEventListener("wheel", function (ev) {
      ev.preventDefault();
      const lay = _layout();
      const rect = canvas.getBoundingClientRect();
      const px = ev.clientX - rect.left;
      if (px < lay.PAD_L || px > lay.PAD_L + lay.plotW) return;
      const cursorX = _xMinFromPx(px, lay.PAD_L, lay.plotW);
      // Wheel up (deltaY < 0) zooms in.
      const factor = ev.deltaY < 0 ? 0.85 : 1.18;
      const newSpan = Math.max(30, Math.min(_CHART_FULL_X_MAX - _CHART_FULL_X_MIN,
        (_vs.xMax - _vs.xMin) * factor));
      const cursorFrac = (cursorX - _vs.xMin) / (_vs.xMax - _vs.xMin);
      let newMin = cursorX - cursorFrac * newSpan;
      let newMax = newMin + newSpan;
      if (newMin < _CHART_FULL_X_MIN) { newMin = _CHART_FULL_X_MIN; newMax = newMin + newSpan; }
      if (newMax > _CHART_FULL_X_MAX) { newMax = _CHART_FULL_X_MAX; newMin = newMax - newSpan; }
      _vs.xMin = newMin; _vs.xMax = newMax;
      _chartPersistView(canvas, _vs);
      _redraw();
    }, { passive: false });
    let _drag = null;
    canvas.addEventListener("pointerdown", function (ev) {
      const lay = _layout();
      const rect = canvas.getBoundingClientRect();
      const px = ev.clientX - rect.left;
      if (px < lay.PAD_L || px > lay.PAD_L + lay.plotW) return;
      _drag = { x: px, xMin0: _vs.xMin, xMax0: _vs.xMax, plotW: lay.plotW };
      canvas.setPointerCapture && canvas.setPointerCapture(ev.pointerId);
      canvas.style.cursor = "grabbing";
    });
    canvas.addEventListener("pointermove", function (ev) {
      const lay = _layout();
      const rect = canvas.getBoundingClientRect();
      const px = ev.clientX - rect.left;
      const py = ev.clientY - rect.top;
      if (_drag) {
        const dx = px - _drag.x;
        const span = _drag.xMax0 - _drag.xMin0;
        const shift = -(dx / _drag.plotW) * span;
        let newMin = _drag.xMin0 + shift;
        let newMax = _drag.xMax0 + shift;
        if (newMin < _CHART_FULL_X_MIN) { newMin = _CHART_FULL_X_MIN; newMax = newMin + span; }
        if (newMax > _CHART_FULL_X_MAX) { newMax = _CHART_FULL_X_MAX; newMin = newMax - span; }
        _vs.xMin = newMin; _vs.xMax = newMax;
        _vs.hoverEtMin = null;
        _chartPersistView(canvas, _vs);
        _redraw();
        return;
      }
      // Hover \u2014 update crosshair if inside the price plot region.
      if (px < lay.PAD_L || px > lay.PAD_L + lay.plotW
          || py < lay.PAD_T || py > lay.PAD_T + lay.plotH * 0.85) {
        if (_vs.hoverEtMin !== null) { _vs.hoverEtMin = null; _redraw(); }
        return;
      }
      const xv = _xMinFromPx(px, lay.PAD_L, lay.plotW);
      _vs.hoverEtMin = xv;
      _vs.hoverPx = px;
      _vs.hoverPy = py;
      _redraw();
    });
    const _endDrag = function (ev) {
      _drag = null;
      canvas.style.cursor = "crosshair";
      try { canvas.releasePointerCapture && canvas.releasePointerCapture(ev.pointerId); } catch (e) {}
    };
    canvas.addEventListener("pointerup", _endDrag);
    canvas.addEventListener("pointercancel", _endDrag);
    canvas.addEventListener("pointerleave", function () {
      if (_vs.hoverEtMin !== null) { _vs.hoverEtMin = null; _redraw(); }
    });
    canvas.addEventListener("dblclick", function () {
      _vs.xMin = _CHART_FULL_X_MIN;
      _vs.xMax = _CHART_FULL_X_MAX;
      _chartPersistView(canvas, _vs);
      _redraw();
    });
    canvas.style.cursor = "crosshair";
  }

  // v5.31.0 \u2014 Open-position lifecycle overlay. Reads payload.lifecycle
  // (entries[], exits[], trail_series[], open[]) and renders:
  //   * Entry triangles (up, green long / red short) labelled side+shares
  //   * Exit triangles (down, color-coded by alarm A1/A2/B/F/EOD/MANUAL)
  //   * Trail-stop staircase (dashed amber step line of trail.stop)
  //   * Stage-transition tick marks (notches where trail.stage changes)
  //   * MAE/MFE shaded band per closed pair (translucent red/green)
  //   * Live position rail (horizontal at entry_price extending to "now") for open[]
  function _drawLifecycleOverlay(ctx, payload, geom) {
    const lc = payload && payload.lifecycle;
    if (!lc || typeof lc !== "object") return;
    const xOf = geom.xOf, yOf = geom.yOf;
    const X_MIN = geom.X_MIN, X_MAX = geom.X_MAX;
    const yMin = geom.yMin, yMax = geom.yMax;
    const utcIsoToEtMin = geom.utcIsoToEtMin;
    const PAD_L = geom.PAD_L, plotW = geom.plotW;
    const inWin = (m) => m !== null && m >= X_MIN && m <= X_MAX;
    const inPrice = (p) => typeof p === "number" && p >= yMin && p <= yMax;

    // Alarm \u2192 color map for exit triangles.
    const ALARM_COLOR = {
      A1: "#fb7185",  // per-trade brake
      A2: "#f97316",  // velocity
      B:  "#a78bfa",  // 9-EMA cross
      F:  "#fbbf24",  // chandelier
      EOD: "#94a3b8",
      MANUAL: "#64748b",
    };

    // MAE/MFE bands (drawn first so triangles sit on top). Backend supplies
    // each exit with ``et_min`` (exit), ``entry_et_min`` (entry), ``price``,
    // ``entry_price``, ``side``, ``mae_bps``/``mfe_bps``.
    const exits = Array.isArray(lc.exits) ? lc.exits : [];
    for (const ex of exits) {
      if (!ex) continue;
      const entMin = (typeof ex.entry_et_min === "number") ? ex.entry_et_min : null;
      const exMin = (typeof ex.et_min === "number") ? ex.et_min : null;
      if (!inWin(entMin) || !inWin(exMin)) continue;
      const ep = ex.entry_price;
      if (!inPrice(ep)) continue;
      const x0 = xOf(entMin), x1 = xOf(exMin);
      // MAE: adverse excursion (below ep for long, above ep for short).
      // Backend reports mae_bps / mfe_bps as positive bps from entry.
      const side = (ex.side || "long").toLowerCase();
      const longSide = side === "long";
      if (typeof ex.mae_bps === "number" && ex.mae_bps > 0) {
        const maePrice = longSide
          ? ep * (1 - ex.mae_bps / 10000)
          : ep * (1 + ex.mae_bps / 10000);
        if (inPrice(maePrice)) {
          ctx.fillStyle = "rgba(239,68,68,0.10)";
          const yA = yOf(ep), yB = yOf(maePrice);
          ctx.fillRect(x0, Math.min(yA, yB), x1 - x0, Math.abs(yB - yA));
        }
      }
      if (typeof ex.mfe_bps === "number" && ex.mfe_bps > 0) {
        const mfePrice = longSide
          ? ep * (1 + ex.mfe_bps / 10000)
          : ep * (1 - ex.mfe_bps / 10000);
        if (inPrice(mfePrice)) {
          ctx.fillStyle = "rgba(34,197,94,0.10)";
          const yA = yOf(ep), yB = yOf(mfePrice);
          ctx.fillRect(x0, Math.min(yA, yB), x1 - x0, Math.abs(yB - yA));
        }
      }
    }

    // v7.61.0 -- trail-stop staircase removed. v10 ORB doesn't trail;
    // its only post-entry stop adjustment is move-to-BE after +1R,
    // which is already shown as a single horizontal "1R" line at
    // entry-time in the main draw path. The legacy trail_series
    // payload (chandelier stops with stage transitions) was a Tiger
    // Sovereign Phase 4 mechanism.

    // Live position rail \u2014 horizontal at entry_price from entry_ts to "now".
    const open = Array.isArray(lc.open) ? lc.open : [];
    if (open.length) {
      // "now" = last bar's et_min, fallback to X_MAX.
      const bars = (payload && Array.isArray(payload.bars)) ? payload.bars : [];
      let nowMin = X_MAX;
      for (let i = bars.length - 1; i >= 0; i--) {
        if (typeof bars[i].et_min === "number") { nowMin = bars[i].et_min; break; }
      }
      for (const op of open) {
        if (!op) continue;
        const m = (typeof op.et_min === "number") ? op.et_min : null;
        if (!inWin(m)) continue;
        if (!inPrice(op.entry_price)) continue;
        ctx.strokeStyle = (op.side || "long").toLowerCase() === "long"
          ? "rgba(62,194,143,0.55)" : "rgba(226,106,106,0.55)";
        ctx.lineWidth = 1;
        ctx.setLineDash([2, 2]);
        ctx.beginPath();
        ctx.moveTo(xOf(m), yOf(op.entry_price));
        ctx.lineTo(xOf(Math.min(nowMin, X_MAX)), yOf(op.entry_price));
        ctx.stroke();
        ctx.setLineDash([]);
      }
    }

    // Entry triangles (up, labelled side+shares). Backend supplies each
    // entry with ``et_min``, ``ts_utc``, ``price``, ``side``.
    const entries = Array.isArray(lc.entries) ? lc.entries : [];
    ctx.font = "10px system-ui, sans-serif";
    ctx.textAlign = "left";
    for (const en of entries) {
      if (!en) continue;
      const m = (typeof en.et_min === "number") ? en.et_min : null;
      if (!inWin(m) || !inPrice(en.price)) continue;
      const x = xOf(m), y = yOf(en.price);
      const longSide = (en.side || "long").toLowerCase() === "long";
      ctx.fillStyle = longSide ? "#34d399" : "#f87171";
      ctx.beginPath();
      ctx.moveTo(x, y - 7); ctx.lineTo(x - 6, y + 5); ctx.lineTo(x + 6, y + 5);
      ctx.closePath();
      ctx.fill();
      const lbl = (longSide ? "L" : "S")
        + (typeof en.shares === "number" ? " " + en.shares : "");
      ctx.fillStyle = "#cbd5e1";
      ctx.fillText(lbl, x + 8, y + 4);
    }

    // Exit triangles (down, color-coded by alarm).
    for (const ex of exits) {
      if (!ex) continue;
      const m = (typeof ex.et_min === "number") ? ex.et_min : null;
      if (!inWin(m) || !inPrice(ex.price)) continue;
      const x = xOf(m), y = yOf(ex.price);
      ctx.fillStyle = ALARM_COLOR[(ex.alarm || "").toUpperCase()] || "#e26a6a";
      ctx.beginPath();
      ctx.moveTo(x, y + 7); ctx.lineTo(x - 6, y - 5); ctx.lineTo(x + 6, y - 5);
      ctx.closePath();
      ctx.fill();
      const lbl = (ex.alarm || "").toUpperCase();
      if (lbl) {
        ctx.fillStyle = "#cbd5e1";
        ctx.fillText(lbl, x + 8, y - 4);
      }
    }
  }

  function _pmtxHydrateIntradayCharts(root) {
    const scope = root || document;
    const sections = scope.querySelectorAll('[data-intraday-chart]');
    sections.forEach(function(section) {
      const tkr = section.getAttribute("data-intraday-chart");
      if (!tkr) return;
      // Skip if already painted within TTL.
      const cached = _intradayCache[tkr];
      const now = Date.now();
      const canvas = section.querySelector('[data-intraday-canvas]');
      const meta = section.querySelector('[data-intraday-meta]');
      if (cached && (now - cached.ts) < _INTRADAY_TTL_MS && cached.payload) {
        if (canvas) {
          // v6.0.0 \u2014 stash ticker + payload on the canvas so the
          // wheel/drag/hover handlers can issue redraws without going
          // back through the fetch path.
          canvas.dataset.intradayTkr = tkr;
          canvas._lastPayload = cached.payload;
          _drawIntradayChart(canvas, cached.payload);
          _wireIntradayChartInteraction(canvas);
        }
        if (meta) {
          const n = (cached.payload.bars || []).length;
          meta.textContent = n + " bars \u00b7 " + (_isMobile() ? "5m" : "1m");
        }
        return;
      }
      fetch("/api/intraday/" + encodeURIComponent(tkr), { credentials: "same-origin" })
        .then(function(r) { return r.ok ? r.json() : null; })
        .then(function(payload) {
          if (!payload || !payload.ok) {
            if (meta) meta.textContent = "chart unavailable";
            return;
          }
          _intradayCache[tkr] = { ts: Date.now(), payload: payload };
          if (canvas) {
            canvas.dataset.intradayTkr = tkr;
            canvas._lastPayload = payload;
            _drawIntradayChart(canvas, payload);
            _wireIntradayChartInteraction(canvas);
          }
          if (meta) {
            const n = (payload.bars || []).length;
            meta.textContent = n + " bars \u00b7 " + (_isMobile() ? "5m" : "1m");
          }
        })
        .catch(function() {
          if (meta) meta.textContent = "chart fetch failed";
        });
    });
  }

  // v7.53.0 -- expose the per-ticker chart pipeline to IIFE 2 so the
  // v10 Proximity Matrix (which lives in the executor-render IIFE)
  // can drop an intraday chart into an expanded row without
  // duplicating the canvas / hydration / interaction code. Single
  // entry point so callers don't depend on the internal helpers.
  // v9.1.14 -- revert v9.1.13's panel cache + transplant. That
  // approach was intended to preserve canvas handlers across parent
  // re-renders, but in practice broke ALL chart interactivity. The
  // exact failure mode was unclear from outside the live browser --
  // possible culprits: pointer-capture being lost during the
  // appendChild move, a layout race after transplant making the
  // canvas have a zero-size getBoundingClientRect at the moment a
  // pointer event arrived, or some interaction with the chart's
  // own state-poll-driven re-hydration. Falling back to the
  // pre-v9.1.13 (and pre-v9.1.12 idempotency) plain rebuild path:
  // every state poll rebuilds the chart fresh. The user's pan/zoom
  // VIEW state is still preserved across rebuilds via the
  // _chartViewByTkr per-ticker dict (set in _chartGetState and
  // updated by _chartPersistView). What's lost is mid-drag
  // continuity -- a drag that straddles a state-poll boundary gets
  // interrupted -- but every fresh interaction starts cleanly and
  // works. Better than v9.1.13's "nothing is interactive".
  if (typeof window !== "undefined") {
    window.__tgRenderTickerChart = function (tkr, containerEl) {
      if (!tkr || !containerEl) return;
      try {
        containerEl.innerHTML = _pmtxIntradayChartPanel(tkr);
        _pmtxHydrateIntradayCharts(containerEl);
      } catch (e) { /* never break the v10 renderer */ }
    };
  }

  // v5.21.0 — Daily SMA stack panel. Renders the new per-row SMA
  // section inside the expanded Titan detail panel, just after the
  // pipeline-components comp-strip. Returns an HTML string.
  // Handles null sma_stack defensively — renders a placeholder instead
  // of crashing. None-safe per window: if smas[window] is null, renders
  // a dash in the swatch column with no gate chip and no delta.
  function _pmtxSmaStackPanel(smaStack) {
    if (!smaStack) {
      return '<div class="pmtx-sma-section pmtx-sma-unavailable">'
        + 'Daily SMA stack \u2014 data not available'
        + '</div>';
    }

    var dc = smaStack.daily_close;
    var smas = smaStack.smas || {};
    var deltasAbs = smaStack.deltas_abs || {};
    var deltasPct = smaStack.deltas_pct || {};
    var above = smaStack.above || {};
    var cls = smaStack.stack_classification || 'mixed';
    var substate = smaStack.stack_substate || 'scrambled';
    var orderChips = smaStack.order_chips || [];
    var orderRelations = smaStack.order_relations || [];

    // --- headline stack pill -------------------------------------------
    var pillLabel;
    var pillCls;
    if (cls === 'bullish') {
      pillLabel = 'Bullish stack';
      pillCls = 'pmtx-sma-stack-pill pmtx-sma-pill-bullish';
    } else if (cls === 'bearish') {
      pillLabel = 'Bearish stack';
      pillCls = 'pmtx-sma-stack-pill pmtx-sma-pill-bearish';
    } else {
      // mixed: map substate
      var substateMap = {
        all_above: 'Above all SMAs',
        all_below: 'Below all SMAs',
        above_short_below_long: 'Above short-term \u00b7 below long-term',
        below_short_above_long: 'Below short-term \u00b7 above long-term',
        scrambled: 'Scrambled'
      };
      pillLabel = substateMap[substate] || 'Scrambled';
      pillCls = 'pmtx-sma-stack-pill pmtx-sma-pill-mixed';
    }

    var pillHtml = '<span class="' + pillCls + '">'
      + '<span class="pmtx-sma-pill-dot"></span>'
      + escapeHtml(pillLabel)
      + '</span>';

    // --- section heading -----------------------------------------------
    var headHtml = '<div class="pmtx-sma-section-head">'
      + '<span>Daily SMA stack</span>'
      + '<span class="pmtx-sma-kbd">new</span>'
      + '<span class="pmtx-sma-help">daily close vs. 12\u00a0/\u00a022\u00a0/\u00a055\u00a0/\u00a0100\u00a0/\u00a0200-day SMA \u00b7 &ldquo;bullish stack&rdquo; when 12&gt;22&gt;55</span>'
      + pillHtml
      + '</div>';

    // --- table header --------------------------------------------------
    var WINDOWS = [12, 22, 55, 100, 200];
    var theadHtml = '<thead><tr>'
      + '<th class="pmtx-sma-label-col">Daily close</th>';
    WINDOWS.forEach(function(w) {
      theadHtml += '<th><span class="pmtx-sma-swatch pmtx-sma-sw-' + w + '"></span>SMA\u00a0' + w + '</th>';
    });
    theadHtml += '</tr></thead>';

    // --- table body row ------------------------------------------------
    var dcStr = (dc !== null && dc !== undefined) ? ('$' + dc.toFixed(2)) : '\u2014';
    var tbodyHtml = '<tbody><tr>'
      + '<td class="pmtx-sma-label-col pmtx-sma-close-cell">' + escapeHtml(dcStr) + '</td>';

    WINDOWS.forEach(function(w) {
      var smaVal = (smas[w] !== undefined && smas[w] !== null) ? smas[w] : null;
      if (smaVal === null) {
        tbodyHtml += '<td><span class="pmtx-sma-none">\u2014</span></td>';
        return;
      }
      var isAbove = above[w];
      var gateClass = isAbove ? 'pmtx-sma-gate pmtx-sma-gate-pass' : 'pmtx-sma-gate pmtx-sma-gate-fail';
      var gateMark = isAbove ? '\u2713' : '\u2717';

      var dAbs = (deltasAbs[w] !== null && deltasAbs[w] !== undefined) ? deltasAbs[w] : null;
      var dPct = (deltasPct[w] !== null && deltasPct[w] !== undefined) ? deltasPct[w] : null;
      var deltaHtml = '';
      if (dAbs !== null && dPct !== null) {
        var sign = dAbs >= 0 ? '+' : '';
        var pctSign = dPct >= 0 ? '+' : '';
        var deltaClass = dAbs >= 0 ? 'pmtx-sma-delta pmtx-sma-delta-pos' : 'pmtx-sma-delta pmtx-sma-delta-neg';
        var absStr = sign + '$' + Math.abs(dAbs).toFixed(2);
        var pctStr = pctSign + (dPct * 100).toFixed(1) + '%';
        deltaHtml = '<span class="' + deltaClass + '">' + escapeHtml(absStr + ' \u00b7 ' + pctStr) + '</span>';
      }

      tbodyHtml += '<td>'
        + '<span class="' + gateClass + '">' + gateMark + '</span>'
        + deltaHtml
        + '</td>';
    });
    tbodyHtml += '</tr></tbody>';

    // --- footer order-line --------------------------------------------
    // Consume order_chips and order_relations to render SMA 12 op SMA 22 op SMA 55
    var opMap = { gt: '>', lt: '<', eq: '=', unknown: '?' };
    var opCssMap = { gt: 'pmtx-sma-order-op-ok', lt: 'pmtx-sma-order-op-bad', eq: 'pmtx-sma-order-op-neut', unknown: 'pmtx-sma-order-op-neut' };

    var footerInner = '<div class="pmtx-sma-order-line">'
      + '<span class="pmtx-sma-order-lbl">Order</span>';

    for (var i = 0; i < orderChips.length; i++) {
      var chip = orderChips[i];
      var chipW = chip.window;
      var chipVal = (chip.value !== null && chip.value !== undefined) ? chip.value.toFixed(2) : '\u2014';
      footerInner += '<span class="pmtx-sma-order-chip">'
        + '<span class="pmtx-sma-swatch pmtx-sma-sw-' + chipW + '"></span>'
        + 'SMA\u00a0' + chipW + '\u00a0\u00b7\u00a0' + escapeHtml(chipVal)
        + '</span>';
      if (i < orderRelations.length) {
        var rel = orderRelations[i];
        var opSym = opMap[rel] || '?';
        var opCls = 'pmtx-sma-order-op ' + (opCssMap[rel] || 'pmtx-sma-order-op-neut');
        footerInner += '<span class="' + opCls + '">' + escapeHtml(opSym) + '</span>';
      }
    }

    // Headline verdict tag (right-aligned)
    if (cls === 'bullish') {
      footerInner += '<span class="pmtx-sma-order-verdict pmtx-sma-order-verdict-bullish">12 &gt; 22 &gt; 55 \u2713</span>';
    } else if (cls === 'bearish') {
      footerInner += '<span class="pmtx-sma-order-verdict pmtx-sma-order-verdict-bearish">12 &lt; 22 &lt; 55 \u2717</span>';
    }

    footerInner += '</div>';

    var tfootHtml = '<tfoot><tr><td colspan="6">' + footerInner + '</td></tr></tfoot>';

    // --- assemble ---------------------------------------------------
    return '<div class="pmtx-sma-section">'
      + headHtml
      + '<div class="pmtx-sma-wrap">'
      + '<table class="pmtx-sma-table">'
      + theadHtml
      + tbodyHtml
      + tfootHtml
      + '</table>'
      + '</div>'
      + '</div>';
  }


  function _pmtxBuildRow(
    tkr, idx, positionsByTicker, tradesByTicker, proximityByTicker,
    longPermit, shortPermit,
    perTickerV510, perPositionV510, regimeBlock, sectionIPermit,
    visibilityOpts
  ) {
    perTickerV510 = perTickerV510 || {};
    perPositionV510 = perPositionV510 || {};
    regimeBlock = regimeBlock || {};
    // v5.29.0 — visibility options drive flag-driven hiding of bypassed
    // components. Defaults preserve legacy behaviour (everything visible)
    // for callers that don't pass an opts object.
    visibilityOpts = visibilityOpts || {};
    const showVolume  = visibilityOpts.showVolume  !== false;
    const showAlarmC  = visibilityOpts.showAlarmC  !== false;
    const showAlarmD  = visibilityOpts.showAlarmD  !== false;
    const showAlarmE  = visibilityOpts.showAlarmE  !== false;
    const showAlarmF  = visibilityOpts.showAlarmF  !== false;
    // v6.1.1 \u2014 v6.1.0 strategy flag block.
    const v610Flags   = visibilityOpts.v610Flags || {};
    // v6.2.0 \u2014 entry-loosening flag block.
    const v620Flags   = visibilityOpts.v620Flags || {};
    // v6.3.0 \u2014 Sentinel B noise-cross filter flag block.
    const v630Flags   = visibilityOpts.v630Flags || {};
    // v6.4.0 \u2014 Alarm B disable + Chandelier multiplier flag block.
    const v640Flags   = visibilityOpts.v640Flags || {};
    const p2 = idx.p2[tkr] || null;
    // v5.21.0 — sma_stack is nested in the phase2 row dict.
    const smaStack = (p2 && p2.sma_stack) ? p2.sma_stack : null;
    // Pick the side that has a permit; if both, prefer LONG.
    const preferSide = longPermit ? "LONG" : (shortPermit ? "SHORT" : "LONG");
    const p3 = idx.p3[tkr + ":" + preferSide] || idx.p3[tkr] || null;
    const p4 = idx.p4[tkr + ":" + preferSide] || idx.p4[tkr] || null;
    const pos = positionsByTicker[tkr] || null;
    const fills = tradesByTicker[tkr] || [];
    const prox = proximityByTicker[tkr] || null;

    // 5m ORB — Phase 2 boundary hold (2 consec above OR_high or below OR_low).
    let orb = null;
    if (p2) {
      orb = !!(p2.two_consec_above || p2.two_consec_below);
    }
    // ADX>20 — not directly exposed; treat as PASS once Phase 3 fires
    // Entry 1 (DI+ > 25 on 5m empirically requires ADX>20). Else pending.
    const adx = (p3 && p3.entry1_fired) ? true : null;
    let di5 = null;
    if (p3 && typeof p3.entry1_di === "number") {
      di5 = (p3.entry1_di > 25);
    }
    // Vol confirm — Phase 2 vol_gate_status.
    let vol = null;
    let volLabel = "";
    if (p2 && p2.vol_gate_status) {
      const vs = String(p2.vol_gate_status).toUpperCase();
      volLabel = "Volume gate: " + vs;
      if (vs === "PASS") vol = true;
      else if (vs === "FAIL") vol = false;
      else if (vs === "COLD") vol = "warn";
      else if (vs === "OFF") vol = null; // gate disabled \u2014 pending dash
    }

    // Strike Cap (count of opens today, max 3).
    let strikesUsed = 0;
    fills.forEach((f) => {
      const a = String((f && (f.action || f.act)) || "").toUpperCase();
      if (a.indexOf("BUY") === 0 || a.indexOf("SELL_SHORT") === 0 || a === "OPEN" || a.indexOf("OPEN_") === 0) {
        strikesUsed += 1;
      }
    });
    if (strikesUsed > 3) strikesUsed = 3;
    const strikeDots = [];
    for (let i = 0; i < 3; i++) {
      const used = i < strikesUsed;
      const lockClass = (strikesUsed === 3 && !pos) ? "pmtx-strike-locked" : "pmtx-strike-used";
      strikeDots.push('<span class="pmtx-strike-dot' + (used ? (" " + lockClass) : "") + '"></span>');
    }
    const strikeHtml = '<span class="pmtx-strike-cap" title="' + strikesUsed + ' of 3 entries used today">' + strikeDots.join("") + '</span>';

    // State pill.
    let stateCls = "pmtx-state-idle";
    let stateTxt = "IDLE";
    if (pos) {
      stateCls = "pmtx-state-inpos";
      stateTxt = "IN POS";
    } else if (strikesUsed >= 3) {
      stateCls = "pmtx-state-locked";
      stateTxt = "LOCKED";
    } else if (orb === true && (longPermit || shortPermit)) {
      stateCls = "pmtx-state-armed";
      stateTxt = "ARMED";
    }
    const stateHtml = '<span class="pmtx-state-pill ' + stateCls + '">' + escapeHtml(stateTxt) + '</span>';

    // Last trade (rendered inside the expand-detail panel only).
    const lastFill = fills.length ? fills[fills.length - 1] : null;
    let lastHtml = '<span class="pmtx-last">\u2014</span>';
    if (lastFill) {
      const px = (typeof lastFill.price === "number") ? fmtPx(lastFill.price) : "\u2014";
      const act = String(lastFill.action || lastFill.act || "").toUpperCase();
      let pnlCls = "";
      if (typeof lastFill.realized_pnl === "number" && lastFill.realized_pnl !== 0) {
        pnlCls = lastFill.realized_pnl > 0 ? " pmtx-pnl-up" : " pmtx-pnl-down";
      }
      lastHtml = '<span class="pmtx-last' + pnlCls + '" title="Most recent fill today">' + escapeHtml(act) + ' @ ' + escapeHtml(px) + '</span>';
    }

    // Titan name only — v5.18.0 dropped the second-line meta string
    // ("long side"/"awaiting permit"/"short side"/"LONG 50sh") because
    // the row tint, State pill, and Price·Distance column already
    // convey the same information and the meta line was eating ~12px
    // of vertical space per Titan.
    const titanHtml = ''
      + '<div class="pmtx-titan">'
      +   '<span class="pmtx-titan-name">' + escapeHtml(tkr) + '</span>'
      + '</div>';

    // v5.18.0 — Price · Distance cell. Replaces the standalone
    // Proximity card. Shows live last + a thin bar that fills as price
    // approaches the nearest OR boundary (0% distance → 100% bar,
    // 2% distance → 0% bar). Bar tints amber inside 0.5%.
    const proxHtml = _pmtxProxCell(prox);

    // Row tint follows Phase 1 permit (matches mockup .permit-go /
    // .permit-block tints).
    let rowTint = "";
    if (longPermit || shortPermit) rowTint = " pmtx-row-permit-go";
    if (strikesUsed >= 3 && !pos)  rowTint = " pmtx-row-permit-block";

    // v5.18.0 — single ~38px row per Titan (mockup spec). The row is
    // clickable when there's something interesting to expand (open
    // position OR a recorded fill today). Detail row is rendered next
    // to it but hidden by default (.pmtx-detail-open toggles via JS).
    // v5.19.3 — a row is also expandable when proximity carries any
    // useful payload (live price, nearest-boundary label, OR_high/OR_low).
    // Pre-market and quiet RTH sessions have no positions or fills yet,
    // so before this fix the matrix had nothing clickable for hours —
    // even though the detail panel surfaces price + boundary info that
    // does exist. Click stays open until the user clicks the same row
    // again (the toggle handler at body level was always doing this;
    // the regression was that hasDetail was false everywhere pre-market
    // so no .pmtx-detail-row was ever rendered).
    // v5.28.1 \u2014 sentinel strip always renders on expanded titan cards.
    // When there is no open position, every alarm cell shows in the idle
    // state with an em-dash placeholder so the user always sees the alarm
    // panel layout (rather than the panel collapsing entirely between
    // sessions). The strip's leading banner indicates "no open position".
    const sentinelStripHtml = _pmtxSentinelStrip(p4, !!pos, {
      showAlarmC: showAlarmC,
      showAlarmD: showAlarmD,
      showAlarmE: showAlarmE,
      showAlarmF: showAlarmF,
      // v6.3.0 \u2014 forward the noise-cross filter block so the
      // B Trend Death cell can append a "\u00b7 noise\u22650.10\u00d7ATR" suffix
      // when the v630 filter is active.
      v630Flags: v630Flags,
      // v6.4.0 \u2014 forward Alarm B / Chandelier flag block so the
      // B Trend Death cell renders the DISABLED state when B is off.
      v640Flags: v640Flags,
    });
    const proxHasDetail = !!(prox && (
      typeof prox.price === "number"
      || prox.nearest_label
      || typeof prox.or_high === "number"
      || typeof prox.or_low === "number"
    ));
    // v5.28.2 — a Titan with an open Phase-1 permit (long or short) is also
    // expandable even before any proximity data has flowed. Without this, a
    // row with .pmtx-row-permit-go tint had hasDetail=false during quiet
    // pre-market windows, so the detail row — which carries the alarm strip
    // surfaced by v5.28.1 — never rendered. The user's complaint that the
    // alarm panel was missing on expanded permit cards traced to exactly
    // this gate, not to the strip function itself.
    const hasDetail = !!(pos || lastFill || proxHasDetail || longPermit || shortPermit);
    const expandIcon = hasDetail
      ? '<span class="pmtx-expand-chev" aria-hidden="true">\u203a</span>'
      : '<span class="pmtx-expand-chev pmtx-expand-empty" aria-hidden="true"></span>';
    const rowAttrs = ' data-pmtx-tkr="' + escapeHtml(tkr) + '"' + (hasDetail ? '' : ' data-pmtx-no-detail="1"');
    // v5.31.5 \u2014 per-stock Weather cell. Lives at position 2, between
    // Titan and Boundary. Glyph maps:
    //   x        \u2014 no permit (long_open=false AND short_open=false AND
    //              the local override would not open either side either)
    //   green up  \u2014 long-aligned local weather (direction=='up')
    //   green dn  \u2014 short-aligned local weather (direction=='down')
    //   em-dash   \u2014 data still warming up / classifier returned 'flat'
    const _wxHtml = _pmtxWeatherCell(
      perTickerV510[tkr] || null,
      !!longPermit,
      !!shortPermit,
    );

    const mainTr = '<tr class="pmtx-row' + rowTint + (hasDetail ? '' : ' pmtx-row-static') + '"' + rowAttrs + '>'
      + '<td class="pmtx-col-titan">' + titanHtml + '</td>'
      + '<td class="pmtx-col-weather">' + _wxHtml + '</td>'
      // v5.20.8 \u2014 column headers renamed to match the gate-card
      // names (Boundary / Momentum / Authority / Volume). The Authority
      // cell (still uses the legacy .pmtx-col-diplus class for layout
      // continuity) now reflects Section-I permit alignment instead of
      // the per-ticker 5m DI\u00b1 gate; per-ticker DI\u00b1 detail
      // lives in the Momentum card metric stack inside the expanded
      // row. The cell is green when at least one side has its permit
      // open, red when both are closed, pending when section_i_permit
      // is unavailable.
      + '<td class="pmtx-col-orb">' + _pmtxGateCell(orb, "Boundary: two consecutive 1m closes through ORH (long) / ORL (short)") + '</td>'
      + (showVolume ? '<td class="pmtx-col-vol">' + _pmtxGateCell(vol, volLabel || "Volume gate (1m vol \u2265 100% of 55-bar avg)") + '</td>' : '')
      + '<td class="pmtx-col-diplus">' + _pmtxGateCell(_pmtxAuthorityCell(sectionIPermit), _pmtxAuthorityTooltip(sectionIPermit)) + '</td>'
      + '<td class="pmtx-col-adx">' + _pmtxGateCell(adx, "Momentum: 5m ADX > 20 (proxied by Phase 3 Entry-1 firing)") + '</td>'
      + '<td class="pmtx-col-strike">' + strikeHtml + '</td>'
      + '<td class="pmtx-col-state">' + stateHtml + '</td>'
      + '<td class="pmtx-col-prox">' + proxHtml + '</td>'
      + '<td class="pmtx-col-mini">' + _pmtxMiniChartCell(perTickerV510[tkr] || null) + '</td>'
      + '<td class="pmtx-col-expand">' + expandIcon + '</td>'
      + '</tr>';
    let tableRows = mainTr;
    if (hasDetail) {
      const detailInner = ''
        + '<div class="pmtx-detail-grid">'
        +   '<div class="pmtx-detail-stat"><div class="l">Last trade today</div><div class="v">' + lastHtml + '</div></div>'
        +   (prox && typeof prox.price === "number"
              ? '<div class="pmtx-detail-stat"><div class="l">Last price</div><div class="v">' + escapeHtml(fmtPx(prox.price)) + '</div></div>'
              : '')
        +   (prox && prox.nearest_label
              ? '<div class="pmtx-detail-stat"><div class="l">Nearest boundary</div><div class="v" title="' + escapeHtml(prox.nearest_label) + '">' + escapeHtml(_pmtxAbbrevBoundary(prox.nearest_label)) + '</div></div>'
              : '')
        +   (prox && (typeof prox.or_high === "number" || typeof prox.or_low === "number")
              ? '<div class="pmtx-detail-stat"><div class="l">OR range</div><div class="v">'
                + escapeHtml(typeof prox.or_low === "number" ? fmtPx(prox.or_low) : "\u2014")
                + ' \u2013 '
                + escapeHtml(typeof prox.or_high === "number" ? fmtPx(prox.or_high) : "\u2014")
                + '</div></div>'
              : '')
        + '</div>'
        // v5.20.3 \u2014 component-state card grid. Replaces the
        // verbose v15.0 spec-definitions <dl>; the spec rules live in
        // tiger_sovereign-spec-v15-1.md and the operator no longer
        // needs to read the entire spec inside every expanded row.
        // Each card: phase chip + name + short description + status
        // badge + numeric value. Phases 1/2/3, alarms (A/B), and the
        // strike counter are surfaced directly from the live indices
        // already computed above (orb, adx, di5, vol, longPermit,
        // shortPermit, strikesUsed, p4 sentinel/titan_grip).
        + _pmtxComponentGrid({
            tkr: tkr,
            longPermit: longPermit,
            shortPermit: shortPermit,
            orb: orb,
            vol: vol,
            volStatus: (p2 && p2.vol_gate_status) || null,
            adx: adx,
            di5: di5,
            di5Val: (p3 && typeof p3.entry1_di === "number") ? p3.entry1_di : null,
            strikesUsed: strikesUsed,
            pos: pos,
            p4: p4,
            // v5.29.0 — component grid honors the same volume-gate flag.
            showVolume: showVolume,
            // v5.20.5 \u2014 numeric metric rows surfaced beneath each card state.
            ptv510: perTickerV510[tkr] || null,
            ppv510: pos
              ? (perPositionV510[tkr + ":" + (pos.side || preferSide).toUpperCase()] || null)
              : null,
            regimeBlock: regimeBlock,
            sectionIPermit: sectionIPermit,
            // v6.1.1 \u2014 strategy flags decorate Phase 2 Boundary + Phase 3
            // Authority cards with active OR-break / EMA-confirm state.
            v610Flags: v610Flags,
            // v6.2.0 \u2014 entry-loosening flags decorate Local Weather,
            // Boundary, and Momentum cards.
            v620Flags: v620Flags,
            // v6.3.0 \u2014 Sentinel B noise-cross filter flags decorate
            // the Local Weather card and the B Trend Death sentinel cell.
            v630Flags: v630Flags,
            // v6.4.0 \u2014 Alarm B disable + Chandelier multiplier flags.
            // The component grid surfaces an ALARM_B_ENABLED line on the
            // Strategy panel and tints the B Trend Death card DISABLED.
            v640Flags: v640Flags,
          })
        // v5.23.2 \u2014 expanded-row scan order: component-state cards
        // (process state at-a-glance) \u2192 sentinel alarm strip (live
        // alarm status for the open position) \u2192 SMA stack (daily
        // structural context) \u2192 intraday chart (today's price action
        // with OR/AVWAP/EMA9 overlays). The chart sits at the bottom
        // because it's the most visually heavy element and operators
        // typically only need it after they've already triaged the
        // alarm/SMA context above. The chart placeholder is hydrated
        // post-render by _pmtxHydrateIntradayCharts() which fetches
        // /api/intraday/{tkr} and paints to a Canvas.
        + (sentinelStripHtml || "")
        + _pmtxSmaStackPanel(smaStack)
        + _pmtxIntradayChartPanel(tkr);
      // v5.29.0 — detail row colspan tracks the visible column count so
      // hiding the Volume column doesn't leave a gap above the detail.
      // v5.31.5 — bumped by one to account for the per-stock Weather
      // column inserted at position 2 in the header above.
      // v6.0.0 — bumped again for the new Trend mini-chart column.
      const _detailColspan = showVolume ? 11 : 10;
      tableRows += '<tr class="pmtx-detail-row" data-pmtx-tkr="' + escapeHtml(tkr) + '">'
        + '<td colspan="' + _detailColspan + '">' + detailInner + '</td></tr>';
    }

    // v5.18.1 \u2014 mobile cards path retired. The same compact table
    // is used at every viewport (CSS hides ADX / DI+5m / Vol-confirm
    // columns on \u2264720px). The `card` field is kept in the return
    // signature for any embedders still calling _pmtxBuildRow directly.
    return { tableRows: tableRows, card: "" };
  }

  // v5.18.0 — single Price · Distance cell, used in the table
  // and inside the mobile card. Folds the retired Proximity card into
  // a one-line representation: live last + a thin proximity bar +
  // % distance to the nearest OR boundary.
  function _pmtxProxCell(prox) {
    if (!prox) return '<span class="pmtx-prox-empty" title="No proximity data yet">\u2014</span>';
    const pct = (prox.nearest_pct !== null && prox.nearest_pct !== undefined && isFinite(prox.nearest_pct))
      ? prox.nearest_pct : null;
    let fill = 0;
    if (pct !== null) {
      // 0% distance → 100% fill, 2% distance → 0% fill.
      fill = Math.max(0, Math.min(100, Math.round((1 - Math.min(pct, 0.02) / 0.02) * 100)));
    }
    const warn = pct !== null && pct < 0.005;
    const px = (typeof prox.price === "number") ? fmtPx(prox.price) : "\u2014";
    const fullLbl = prox.nearest_label || "\u2014";
    const lbl = _pmtxAbbrevBoundary(fullLbl) || "\u2014";
    const pctText = pct !== null ? (pct * 100).toFixed(2) + "% \u00b7 " + escapeHtml(lbl) : escapeHtml(lbl);
    // Tooltip keeps the full "OR-high" / "OR-low" label so the operator
    // sees the unambiguous boundary name on hover.
    const titleText = pct !== null
      ? "Last " + px + " \u2014 " + (pct * 100).toFixed(3) + "% from " + fullLbl
      : "Last " + px;
    return '<div class="pmtx-prox" title="' + escapeHtml(titleText) + '">'
      +    '<span class="pmtx-prox-price">' + escapeHtml(px) + '</span>'
      +    '<div class="pmtx-prox-bar"><div class="pmtx-prox-fill ' + (warn ? "warn" : "ok") + '" style="width:' + fill + '%"></div></div>'
      +    '<span class="pmtx-prox-pct' + (warn ? " pmtx-prox-warn" : "") + '">' + pctText + '</span>'
      +  '</div>';
  }

  // v5.21.0 — Helper: pick new vAA-1 alarm sub-dict from sentinel object,
  // with graceful fall-back to legacy flat keys when new key is absent.
  // newKey   : e.g. "a_loss"    (sentinel sub-dict key)
  // legacyMap: e.g. { pnl: "a1_pnl", threshold: "a1_threshold" }
  //            maps sub-dict field names to the legacy flat key names.
  // Returns the resolved sub-dict (may have null values if all legacy
  // lookups also miss).
  function _pmtxPickAlarm(sen, newKey, legacyMap) {
    // Prefer the vAA-1 sub-dict when it is a real object.
    const newVal = sen[newKey];
    if (newVal && typeof newVal === "object") return newVal;
    // Fall back: build a minimal object from legacy flat keys.
    const out = {};
    for (const [field, legKey] of Object.entries(legacyMap || {})) {
      const v = sen[legKey];
      out[field] = (v !== undefined) ? v : null;
    }
    return out;
  }

  // v5.21.0 — Helper: derive a state class from a resolved alarm
  // sub-dict.
  //
  // v5.22.0 — traffic-light semantics. The strip now answers "how
  // close is this alarm to firing?" at a glance:
  //
  //   safe  (green)  = armed, far from threshold (>25% headroom)
  //   warn  (yellow) = armed, within 25% of threshold (>= 75% of trigger)
  //   trip  (red)    = triggered (already fired, exit issued)
  //   idle  (gray)   = not in position / no data yet
  //
  // Each alarm decides its own warn band because the units differ:
  //   A1 Loss          : pnl    vs threshold (both negative dollars)
  //   A2 Flash         : velocity_pct vs threshold_pct (negative ratios)
  //   B Trend Death    : delta = close - ema9; warn when |delta| within
  //                      0.25% of cross relative to close
  //   C Vel. Ratchet   : 2-of-3 strictly-decreasing ADX values = warn
  //   D HVP Lock       : ratio (current/peak) vs threshold_ratio (0.75);
  //                      warn when ratio between threshold and
  //                      threshold/0.75*0.85 (i.e. ratio in [0.75, 0.85])
  //   E Divergence Trap: armed-not-triggered = warn (divergence forming)
  function _pmtxAlarmStateClass(alarm, kind) {
    if (!alarm || typeof alarm !== "object") return "idle";
    if (alarm.triggered) return "trip";
    if (!alarm.armed) return "idle";
    // Armed-but-not-triggered: distinguish safe (green) from warn (yellow).
    const WARN_FRACTION = 0.75; // within 25% of threshold = yellow
    switch (kind) {
      case "a_loss": {
        const pnl = (typeof alarm.pnl === "number") ? alarm.pnl : null;
        const th = (typeof alarm.threshold === "number") ? alarm.threshold : -500;
        if (pnl === null) return "safe";
        // Both pnl and threshold are negative; alarm fires when pnl <= th.
        // Yellow when pnl <= 0.75 * th (e.g. <= -$375 for th=-$500).
        return (pnl <= WARN_FRACTION * th) ? "warn" : "safe";
      }
      case "a_flash": {
        const v = (typeof alarm.velocity_pct === "number") ? alarm.velocity_pct : null;
        const th = (typeof alarm.threshold_pct === "number") ? alarm.threshold_pct : -0.01;
        if (v === null) return "safe";
        return (v <= WARN_FRACTION * th) ? "warn" : "safe";
      }
      case "b_trend_death": {
        const close = (typeof alarm.close === "number") ? alarm.close : null;
        const ema9 = (typeof alarm.ema9 === "number") ? alarm.ema9 : null;
        const delta = (typeof alarm.delta === "number") ? alarm.delta
          : (close !== null && ema9 !== null) ? (close - ema9) : null;
        if (close === null || delta === null || close === 0) return "safe";
        // Warn when |delta| relative to close is within 0.25% (close to crossing).
        return (Math.abs(delta) / Math.abs(close) <= 0.0025) ? "warn" : "safe";
      }
      case "c_velocity_ratchet": {
        const win = Array.isArray(alarm.adx_window) ? alarm.adx_window : [];
        // Warn when 2-of-3 consecutive declines exist (last decline pending).
        if (win.length >= 3) {
          const a = win[0], b = win[1], c = win[2];
          if (typeof a === "number" && typeof b === "number" && typeof c === "number") {
            const declines = (a > b ? 1 : 0) + (b > c ? 1 : 0);
            if (declines >= 1 && !alarm.monotone_decreasing) return "warn";
          }
        }
        return "safe";
      }
      case "d_hvp_lock": {
        const ratio = (typeof alarm.ratio === "number") ? alarm.ratio : null;
        const th = (typeof alarm.threshold_ratio === "number") ? alarm.threshold_ratio : 0.75;
        if (ratio === null) return "safe";
        // Triggered when ratio < th. Warn when ratio is between th and
        // th/WARN_FRACTION (i.e. within 25% above the trigger band).
        const warnHi = th / WARN_FRACTION; // e.g. 0.75 / 0.75 = 1.0; cap below.
        return (ratio >= th && ratio <= Math.min(warnHi, th + 0.10)) ? "warn" : "safe";
      }
      case "e_divergence_trap": {
        // Spec: armed-not-triggered means divergence is forming.
        // Treat that as warn so operators see it brewing.
        return "warn";
      }
      default:
        return "safe";
    }
  }

  // v5.21.0 — Inline 5-cell sentinel strip rendered under any open-position
  // row. Labels and data sources follow vAA-1 spec (tiger_sovereign_spec_vAA-1
  // Section 5). New keys preferred; legacy keys used as fallback during the
  // deploy window when new backend fields are not yet present.
  //
  // Alarm exit classification per spec Section 5 architectural rule:
  //   A1/A2/B/D -> MARKET EXIT   |   C/E -> STOP MARKET ratchets
  function _pmtxSentinelStrip(p4, hasPos, opts) {
    // v5.28.1 \u2014 hasPos: when false, every cell is forced to the idle
    // state with an em-dash value, and a small banner labels the strip
    // as "no open position". The cell layout is kept identical so users
    // see the same six alarms in the same order regardless of session
    // state. When true, behaves exactly as before: cells reflect live
    // sentinel data from p4.sentinel.
    // v5.29.0 \u2014 opts.showAlarm{C,D,E} hide the corresponding cells when
    // the matching ALARM_*_ENABLED flag is false (production default for
    // C / D / E since v5.28.0). Defaults preserve legacy behaviour
    // (everything visible) for callers that don't pass opts.
    // v5.30.0 \u2014 opts.showAlarmF surfaces the chandelier trail cell
    // (canonical position: between B and C, i.e. spec ordering A1/A2/B/F/C/D/E).
    // Reads p4.sentinel.f_chandelier (stage / peak_close / proposed_stop)
    // emitted by v5_13_2_snapshot.py. Default visible.
    if (hasPos === undefined) hasPos = true;
    opts = opts || {};
    const showAlarmC = (opts.showAlarmC !== false);
    const showAlarmD = (opts.showAlarmD !== false);
    const showAlarmE = (opts.showAlarmE !== false);
    const showAlarmF = (opts.showAlarmF !== false);
    // v6.3.0 \u2014 noise-cross filter flag block. When enabled and atr_k > 0,
    // append a "\u00b7 noise\u22650.10\u00d7ATR" suffix to the B Trend Death cell
    // value so the operator can see the active threshold without diving
    // into /api/state. Defaults to off when block is missing (older deploys).
    const _v630Flags = opts.v630Flags || {};
    const _v630NoiseCross = !!_v630Flags.noise_cross_filter_enabled;
    const _v630NoiseK = (typeof _v630Flags.noise_cross_atr_k === "number")
      ? _v630Flags.noise_cross_atr_k : 0;
    // v6.4.0 \u2014 Alarm B disable flag. When alarm_b_enabled=false, render
    // the B Trend Death cell in a DISABLED state regardless of live close /
    // EMA9 values: the alarm function is not called server-side so the
    // armed/triggered booleans are stale. Pre-v6.4.0 deploys default to
    // True for safety (preserves legacy rendering).
    const _v640Flags = opts.v640Flags || {};
    const _v640AlarmBEnabled = (typeof _v640Flags.alarm_b_enabled === "boolean")
      ? _v640Flags.alarm_b_enabled : true;
    const sen = (p4 && p4.sentinel) || {};

    // --- Cell A1: Loss ---
    // vAA-1 SENT-A_LOSS: unrealized PnL <= -$500 -> MARKET EXIT.
    const aLoss = _pmtxPickAlarm(sen, "a_loss", {
      pnl: "a1_pnl",
      threshold: "a1_threshold"
    });
    const aLossPnl = (typeof aLoss.pnl === "number") ? aLoss.pnl : null;
    const aLossTh  = (typeof aLoss.threshold === "number") ? aLoss.threshold : -500;
    // Ensure armed/triggered are coherent when falling back to legacy.
    if (aLoss.armed === null || aLoss.armed === undefined) {
      aLoss.armed     = (aLossPnl !== null);
      aLoss.triggered = (aLossPnl !== null && aLossPnl <= aLossTh);
    }
    const a1State  = _pmtxAlarmStateClass(aLoss, "a_loss");
    const a1Val    = _pmtxMoney(aLossPnl) + " / " + _pmtxMoney(aLossTh);

    // --- Cell A2: Flash ---
    // vAA-1 SENT-A_FLASH: 60-second PnL velocity <= -1.0% of position value.
    // The new key stores velocity_pct (a plain ratio, e.g. -0.013 = -1.3%).
    // Legacy key a2_velocity was a per-second rate (units: pnl/s, NOT a %);
    // reconciling units is unsafe, so we default to "—" when only the legacy
    // key is present and the new sub-dict is absent.
    // TODO(v5.21.0): remove legacy fallback once deploy window closes.
    const aFlash = _pmtxPickAlarm(sen, "a_flash", {
      // Intentionally no legacy map here — unit mismatch makes the legacy
      // a2_velocity value misleading as a percent. Prefer safe "—".
      velocity_pct: null,
      threshold_pct: null
    });
    const a2VelPct = (typeof aFlash.velocity_pct === "number") ? aFlash.velocity_pct : null;
    const a2ThPct  = (typeof aFlash.threshold_pct === "number") ? aFlash.threshold_pct : -0.01;
    if (aFlash.armed === null || aFlash.armed === undefined) {
      aFlash.armed     = (a2VelPct !== null);
      aFlash.triggered = (a2VelPct !== null && a2VelPct <= a2ThPct);
    }
    const a2State = _pmtxAlarmStateClass(aFlash, "a_flash");
    const a2Val   = (a2VelPct !== null)
      ? (_pmtxNum(a2VelPct * 100, 2) + "% / " + _pmtxNum(a2ThPct * 100, 2) + "%")
      : "\u2014";

    // --- Cell B: Trend Death ---
    // vAA-1 SENT-B: 5m QQQ close crosses 9-EMA in the adverse direction.
    const bTrend = _pmtxPickAlarm(sen, "b_trend_death", {
      close: "b_close",
      ema9:  "b_ema9",
      delta: "b_delta"
    });
    const bClose = (typeof bTrend.close === "number") ? bTrend.close : null;
    const bEma9  = (typeof bTrend.ema9  === "number") ? bTrend.ema9  : null;
    const bDelta = (typeof bTrend.delta === "number") ? bTrend.delta : null;
    if (bTrend.armed === null || bTrend.armed === undefined) {
      bTrend.armed     = (bClose !== null && bEma9 !== null);
      bTrend.triggered = false; // legacy sentinel does not record a triggered flag
    }
    let bState = _pmtxAlarmStateClass(bTrend, "b_trend_death");
    let bVal   = (bClose !== null && bEma9 !== null)
      ? ("close=" + _pmtxNum(bClose) + " / ema=" + _pmtxNum(bEma9) +
         (bDelta !== null ? " / \u0394=" + _pmtxNum(bDelta) : ""))
      : "\u2014";
    // v6.4.0 \u2014 when ALARM_B_ENABLED=false the alarm is not evaluated;
    // override the cell to a DISABLED state with a clear label and skip the
    // v6.3.0 noise-cross suffix (which would imply the cross can still fire).
    // The state class "disabled" maps to a dim grey pill in app.css and is
    // already used by the C/D/E cells when those alarms are bypassed.
    if (!_v640AlarmBEnabled) {
      // v6.4.0 \u2014 reuse the existing "idle" cell theme (dim grey) plus an
      // explicit textual marker so the operator can immediately see the
      // alarm is not evaluating server-side. Avoid inventing a new CSS
      // class so existing skins keep working without a stylesheet change.
      bState = "idle";
      bVal = "DISABLED \u00b7 ALARM_B_ENABLED=false";
    } else if (_v630NoiseCross && _v630NoiseK > 0 && bVal !== "\u2014") {
      // v6.3.0 \u2014 surface the active noise-cross threshold so the operator
      // can see at a glance that an EMA cross will only fire after adverse
      // drawdown clears k\u00d7ATR (1m). Filter sits in front of the cross
      // and does NOT reset the counter when blocked.
      bVal = bVal + " \u00b7 noise\u2265" + _v630NoiseK.toFixed(2) + "\u00d7ATR";
    }

    // --- Cell F: Chandelier Trail (v5.30.0) ---
    // Alarm F (engine.alarm_f_trail) ratchets a stop on top of the live
    // position. Stage 0 INACTIVE / 1 BREAKEVEN / 2 CHANDELIER_WIDE /
    // 3 CHANDELIER_TIGHT. Armed at stage >= 1; the broker stop-cross
    // realises the exit, so "triggered" is left False in the snapshot.
    // Cell colours: idle when stage 0, warn when stages 1\u20133 (active
    // trail in place but not yet hit), trip never (the close-bar exit
    // path closes the position before the snapshot updates).
    const fChand     = _pmtxPickAlarm(sen, "f_chandelier", {});
    const fStage     = (typeof fChand.stage === "number") ? fChand.stage : 0;
    const fStageName = (typeof fChand.stage_name === "string") ? fChand.stage_name : "INACTIVE";
    const fPeak      = (typeof fChand.peak_close === "number") ? fChand.peak_close : null;
    const fStop      = (typeof fChand.proposed_stop === "number") ? fChand.proposed_stop : null;
    // v6.1.1 \u2014 ATR-trail width fields, surfaced when _V610_ATR_TRAIL_ENABLED.
    const fAtrVal    = (typeof fChand.atr_value === "number" && fChand.atr_value > 0) ? fChand.atr_value : null;
    const fAtrMult   = (typeof fChand.atr_mult  === "number" && fChand.atr_mult  > 0) ? fChand.atr_mult  : null;
    if (fChand.armed === null || fChand.armed === undefined) {
      fChand.armed     = fStage >= 1;
      fChand.triggered = false;
    }
    const fState = _pmtxAlarmStateClass(fChand, "f_chandelier");
    let fVal;
    if (fStage <= 0) {
      fVal = "\u2014";
    } else {
      const _stageLabel = { 1: "BE", 2: "WIDE", 3: "TIGHT" }[fStage] || fStageName;
      const _stopStr = (fStop !== null) ? _pmtxMoney(fStop) : "\u2014";
      const _peakStr = (fPeak !== null) ? _pmtxMoney(fPeak) : "\u2014";
      // v6.1.1 \u2014 append the active ATR width when stage 2/3.
      // Format: "WIDE \u00b7 stop $103.20 / peak $104.10 \u00b7 2.0x ATR ($0.45)".
      // Falls back gracefully when the trail predates v6.1.1 deploy.
      let _atrSuffix = "";
      if (fAtrMult !== null && fAtrVal !== null) {
        _atrSuffix = " \u00b7 " + _pmtxNum(fAtrMult, 1) + "x ATR (" + _pmtxMoney(fAtrVal) + ")";
      }
      fVal = _stageLabel + " \u00b7 stop " + _stopStr + " / peak " + _peakStr + _atrSuffix;
    }

    // --- Cell C: Velocity Ratchet ---
    // vAA-1 SENT-C: three strictly-decreasing 1m ADX values -> STOP MARKET.
    const cRatchet = _pmtxPickAlarm(sen, "c_velocity_ratchet", {});
    const cWindow  = Array.isArray(cRatchet.adx_window) ? cRatchet.adx_window : [null, null, null];
    const cStop    = (typeof cRatchet.stop_price === "number") ? cRatchet.stop_price : null;
    const cState   = _pmtxAlarmStateClass(cRatchet, "c_velocity_ratchet");
    let cAdxStr = cWindow.map(v => (v !== null && v !== undefined) ? _pmtxNum(v, 1) : "\u2014").join("\u2192");
    let cVal    = "adx [" + cAdxStr + "]";
    if (cRatchet.triggered && cStop !== null) cVal += " \u2192 stop " + _pmtxMoney(cStop);

    // --- Cell D: HVP Lock ---
    // vAA-1 SENT-D: current 5m ADX < 75% of Trade_HVP -> MARKET EXIT.
    // Real data now flows from backend (d_hvp_lock sub-dict).
    const dHvp       = _pmtxPickAlarm(sen, "d_hvp_lock", {});
    const dCur5m     = (typeof dHvp.current_5m_adx === "number") ? dHvp.current_5m_adx : null;
    const dTradeHvp  = (typeof dHvp.trade_hvp      === "number") ? dHvp.trade_hvp      : null;
    const dRatio     = (typeof dHvp.ratio           === "number") ? dHvp.ratio          : null;
    const dState     = _pmtxAlarmStateClass(dHvp, "d_hvp_lock");
    const dVal       = (dCur5m !== null && dTradeHvp !== null && dRatio !== null)
      ? ("ADX " + _pmtxNum(dCur5m, 1) + " / peak " + _pmtxNum(dTradeHvp, 1) +
         " (" + _pmtxNum(dRatio * 100, 0) + "%)")
      : "\u2014";

    // --- Cell E: Divergence Trap ---
    // vAA-1 SENT-E: price extreme + RSI divergence -> blocks S2/S3 or ratchets stop.
    // Real data now flows from backend (e_divergence_trap sub-dict).
    const eTrap    = _pmtxPickAlarm(sen, "e_divergence_trap", {});
    const eState   = _pmtxAlarmStateClass(eTrap, "e_divergence_trap");
    let eVal;
    if (eTrap.triggered) {
      if (eTrap.pre_blocked_for_strike !== null && eTrap.pre_blocked_for_strike !== undefined) {
        eVal = "blocks S" + eTrap.pre_blocked_for_strike;
      } else if (eTrap.post_ratchet_stop !== null && eTrap.post_ratchet_stop !== undefined) {
        eVal = "stop " + _pmtxMoney(eTrap.post_ratchet_stop);
      } else {
        eVal = "triggered";
      }
    } else if (eTrap.armed) {
      const eCurRsi  = (typeof eTrap.current_rsi_15   === "number") ? eTrap.current_rsi_15   : null;
      const ePeakRsi = (typeof eTrap.stored_peak_rsi  === "number") ? eTrap.stored_peak_rsi  : null;
      eVal = (eCurRsi !== null && ePeakRsi !== null)
        ? ("RSI " + _pmtxNum(eCurRsi, 1) + " / peak " + _pmtxNum(ePeakRsi, 1))
        : "\u2014";
    } else {
      eVal = "\u2014";
    }

    function cell(label, subtitle, val, state) {
      return '<div class="pmtx-sentinel-cell pmtx-sen-' + state + '">'
        +   '<span class="pmtx-sen-letter">' + escapeHtml(label) + '</span>'
        +   '<span class="pmtx-sen-name">' + escapeHtml(subtitle) + '</span>'
        +   '<span class="pmtx-sen-val">' + escapeHtml(val) + '</span>'
        + '</div>';
    }

    // v5.28.1 \u2014 when no open position, force every cell to idle state
    // with an em-dash value. Layout stays identical so the panel never
    // appears "missing". When in position, the per-cell live values and
    // states computed above are used.
    let _a1State = a1State, _a1Val = a1Val;
    let _a2State = a2State, _a2Val = a2Val;
    let _bState  = bState,  _bVal  = bVal;
    let _fState  = fState,  _fVal  = fVal;
    let _cState  = cState,  _cVal  = cVal;
    let _dState  = dState,  _dVal  = dVal;
    let _eState  = eState,  _eVal  = eVal;
    if (!hasPos) {
      _a1State = _a2State = _bState = _fState = _cState = _dState = _eState = "idle";
      _a1Val   = _a2Val   = _bVal   = _fVal   = _cVal   = _dVal   = _eVal   = "\u2014";
    }

    const banner = hasPos
      ? ""
      : '<div class="pmtx-sentinel-banner"'
        + ' title="No open position \u2014 alarms idle. Will arm when the bot enters.">'
        +   'no open position \u00b7 alarms idle'
        + '</div>';

    return '<div class="pmtx-sentinel-strip-wrap">'
      + banner
      + '<div class="pmtx-sentinel-strip"'
      +   ' title="Sentinel Loop \u2014 all 6 alarms evaluated in parallel.'
      +   ' A1/A2/B/D trigger MARKET EXIT; C/E trigger STOP MARKET ratchets.">'
      +   cell("A1 Loss",       "Per-position $ stop",    _a1Val, _a1State)
      +   cell("A2 Flash",      "1-min adverse %",        _a2Val, _a2State)
      +   cell("B Trend Death", "5m close vs 9-EMA",      _bVal,  _bState)
      +   (showAlarmF ? cell("F Chandelier",  "BE/wide/tight trail",     _fVal,  _fState) : '')
      +   (showAlarmC ? cell("C Vel. Ratchet","3 declining 1m ADX",     _cVal,  _cState) : '')
      +   (showAlarmD ? cell("D HVP Lock",    "5m ADX < 75% peak",      _dVal,  _dState) : '')
      +   (showAlarmE ? cell("E Div. Trap",   "Price extreme + RSI div", _eVal, _eState) : '')
      + '</div>'
      + '</div>';
  }

  // v5.17.0 — next-scan countdown shim. The legacy renderGates body
  // (per-ticker gate panel) was retired with the move to the Permit
  // Matrix; we keep this thin reader so the LIVE pill in the brand
  // row still updates.
  function renderNextScanCountdown(s) {
    // v9.1.56 -- use last_scan_at (absolute UTC) instead of relative
    // next_scan_sec. SSE and scan run on independent 15s timers that drift;
    // SSE can fire 13s before the scan delivering next_scan_sec=2, causing
    // the counter to hit 0 in 2s then sit at ... for 13s. Absolute
    // timestamps let the client compute remaining fresh every second.
    const g = (s && s.gates) || {};
    if (g.last_scan_at) {
      window.__lastScanAt = new Date(g.last_scan_at).getTime();
    }
    if (typeof g.scan_interval_sec === "number" && g.scan_interval_sec > 0) {
      window.__scanIntervalMs = g.scan_interval_sec * 1000;
    }
    updateNextScanLabel();
  }

  function updateNextScanLabel() {
    const el = $("h-tick");
    if (!el) return;
    if (window.__lastScanAt && window.__scanIntervalMs) {
      // Clamp at 1 so the counter never shows ··· (which looks frozen).
      // Shows "01s" for ~1s while the scan fires, then resets to ~14s.
      const _raw = Math.ceil(
        (window.__scanIntervalMs - (Date.now() - window.__lastScanAt)) / 1000
      );
      const remaining = Math.max(0, _raw);
      const ss = String(remaining).padStart(2, "0");
      el.textContent = `♻ ${ss}s`;
      el.setAttribute("aria-label", remaining <= 0 ? "scanning now" : `next scan in ${ss}s`);
      el.setAttribute("title", remaining <= 0 ? "scan in progress" : `next scan in ${ss}s`);
    } else {
      el.textContent = "♻ --";
      el.setAttribute("aria-label", "next scan: not scheduled");
      el.setAttribute("title", "next scan: not scheduled (market closed or scanner idle)");
    }
  }

  function renderHeader(s) {
    const ver = `v${s.version || "?"}`;
    const verEl = document.getElementById("tg-brand-ver");
    if (verEl) verEl.textContent = ver;
    // v5.25.0 — Enabled-executor chips. Reads s.executors_status
    // (added in dashboard_server._executors_status_snapshot) and lights
    // up one chip per executor when its bootstrap returned a non-None
    // instance. Dim chip = disabled (missing PAPER_KEY or *_ENABLED=0).
    // v6.11.9 — the brand-row chips are now hidden (display:none on
    // #tg-exec-chips). The same enabled/disabled signal drives the
    // ✓ / ✗ mark inside each Val / Gene tab heading instead
    // (#tg-badge-val / #tg-badge-gene). The chip DOM is still kept
    // up-to-date so future tooltips / a11y readers can use the same
    // hook without a JS rewrite if Val ever wants the chips back.
    const execStatus = (s && s.executors_status) || {};
    ["val", "gene"].forEach((name) => {
      const chip = document.getElementById(`tg-exec-chip-${name}`);
      const entry = execStatus[name] || { enabled: false, mode: null };
      const enabled = !!entry.enabled;
      const mode = entry.mode || "";
      const label = name.charAt(0).toUpperCase() + name.slice(1);
      // (1) Hidden brand-row chip — update for back-compat / future use.
      if (chip) {
        const mark = chip.querySelector(".tg-exec-mark");
        if (enabled) {
          chip.classList.remove("tg-exec-off");
          chip.classList.add("tg-exec-on");
          if (mark) mark.textContent = "\u2713";
        } else {
          chip.classList.remove("tg-exec-on");
          chip.classList.add("tg-exec-off");
          if (mark) mark.textContent = "\u2014";
        }
      }
      // (2) Tab-heading badge \u2014 visible surface in v6.11.9.
      // v7.88.0 -- stabilized format. Pre-v7.88.0 the badge briefly
      // showed just "\u2713" before the mode resolved to "P" or "L",
      // producing a visible jump operator described as "keeps jumping
      // between P and a paper icon". New format always renders a
      // complete label, matching the Main tab's "\ud83d\udcc4 Paper" style:
      //   \ud83d\udcc4 Paper  = enabled, paper broker
      //   \ud83d\udd34 Live   = enabled, live broker
      //   \u2717         = disabled (mode unknown OR keys unset)
      // The whole label is rendered in one innerHTML write so there's
      // no intermediate state during polling.
      const badge = document.getElementById(`tg-badge-${name}`);
      if (badge) {
        // Read count stored by renderBadge (IIFE-2) so both write paths
        // stay in sync — prevents the count flickering out on every state poll.
        const _posN = ((window.__tgExecPosN || {})[name]) || 0;
        const _posTag = _posN > 0
          ? `<span style="color:#fbbf24;font-weight:600;margin-left:5px">${_posN}</span>`
          : "";
        if (enabled) {
          const isLive = (mode === "live");
          if (isLive) {
            // Dark-green dot + "live" text — readable on dark theme without being garish.
            badge.innerHTML =
              '<span style="color:#22c55e;font-size:8px;vertical-align:middle">●</span>' +
              '<span style="color:#86efac;font-size:10px;font-weight:500;margin-left:3px">live</span>' +
              _posTag;
            badge.setAttribute(
              "title", `${label} executor enabled (live mode)` + (_posN > 0 ? ` · ${_posN} open` : ""),
            );
          } else {
            badge.innerHTML =
              '<span style="color:#5b6572;font-size:10.5px" title="Paper-trading mode">📄 Paper</span>' +
              _posTag;
            badge.setAttribute(
              "title", `${label} executor enabled (paper mode)` + (_posN > 0 ? ` · ${_posN} open` : ""),
            );
          }
          badge.style.color = "";
        } else {
          badge.innerHTML = "✗";
          badge.style.color = "#9aa6b2"; // dim grey
          badge.setAttribute(
            "title",
            `${label} executor disabled (missing PAPER_KEY or *_ENABLED=0)`
          );
        }
      }
    });
    // v9.1.27 -- post-loss cooldown chip restored. Shows count of active
    // (ticker, side) cooldowns across all portfolios. Hidden when 0.
    // Cooldown is enforced for Main via broker/orders.py and for Val/Gene
    // via executors/base.py (wired v9.1.27). Backend emits
    // active_cooldowns_by_portfolio: {main:[...], val:[...], gene:[...]}.
    // v9.1.111: cooldown chip now covers both loss (asymmetric) and sym
    // (post-trade symmetric) cooldowns. kind="loss"|"sym" from API.
    var cdByPid = s.active_cooldowns_by_portfolio || {};
    var cdAll = (cdByPid.main || []).concat(cdByPid.val || []).concat(cdByPid.gene || []);
    var cdN = cdAll.length;
    var cdPill = document.getElementById("v10-cooldown-pill");
    var cdDiv  = document.getElementById("v10-cooldown-pill-divider");
    if (cdPill && cdDiv) {
      if (cdN > 0) {
        var cdLines = cdAll.map(function(c) {
          var rem = c.remaining_sec || 0;
          var remMin = Math.ceil(rem / 60);
          var kind = c.kind === "sym" ? "sym" : "loss";
          var detail = c.kind === "sym"
            ? (c.window_min || 0) + "min sym"
            : "$" + (c.loss_pnl || 0).toFixed(0);
          return kind + " | " + c.ticker + " " + (c.side || "").toUpperCase()
            + " – " + remMin + "min left (" + detail + ")";
        });
        cdPill.textContent = "cooldown " + cdN;
        cdPill.title = "Cooldown active – entry blocked:\n" + cdLines.join("\n");
        cdPill.style.display = "";
        cdDiv.style.display = "";
      } else {
        var _mode = ((s.regime || {}).mode || "CLOSED");
        var _isRth = (_mode === "OPEN" || _mode === "OR" || _mode === "POWER");
        if (_isRth) {
          cdPill.textContent = "cooldown 0";
          cdPill.title = "No active cooldowns.";
          cdPill.style.background = "transparent";
          cdPill.style.color = "var(--text-dim)";
          cdPill.style.display = "";
          cdDiv.style.display = "";
        } else {
          cdPill.style.display = "none";
          cdDiv.style.display = "none";
        }
      }
    }

    // v4.2.2 — extract tz token (ET/CDT/CT/PT/PST/\u2026) from
    // server_time_label tail, e.g. "Fri Apr 24 | 13:09:13 ET".
    // The client-side tick loop renders the actual HH:MM:SS every
    // second; we only cache the tz label here so the clock shows it.
    const lbl = s.server_time_label || "";
    const m = lbl.match(/\d{1,2}:\d{2}:\d{2}\s+([A-Z]{2,4})\s*$/);
    if (m) window.__tgClockTz = m[1];
    if (typeof window.__tgTickClock === "function") window.__tgTickClock();
  }

  // v5.23.0 — renderLastSignal removed. The Last signal card was
  // backed by an in-memory global that resets on every redeploy,
  // so the field was almost always null even when positions were
  // open. Card removed from Main, Val, and Gene tabs.

  // v6.18.0 — Earnings Watcher panel renderer.
  //
  // Header is intentionally compact (2-3 lines):
  //   line 1: title + window pill + status chip
  //   line 2: headline metrics (watched / evaluated / signals / fills)
  //   line 3: expand-disclosure caret ("▸ details")
  //
  // Full detail (cycle telemetry, watched tickers, skip reasons, open
  // positions table) lives in #ew-details and is hidden by default.
  // Click on the card head toggles aria-expanded + the hidden attribute.
  //
  // Self-contained — if any DOM hook is missing the rest still runs.
  // v7.58.0 -- renderEarningsWatcher removed (vestigial: card HTML deleted in this PR).
  // Helper: render one labeled telemetry cell.
  function _ewTelem(label, value) {
    return '<div><div style="font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--text-dim);margin-bottom:1px">' +
      escapeHtml(label) +
      '</div><div class="mono" style="font-size:13px;color:var(--text)">' +
      escapeHtml(value) +
      '</div></div>';
  }

  // v6.18.0 — relocate the EW <section> based on current_window.
  //   premarket / afterhours / closed -> just below #pos-body's section
  //                                      (so it sits right under Open positions)
  //   rth_idle (RTH)                  -> bottom of <main>
  //
  // Idempotent: only moves if the current parent/sibling differs from
  // the desired anchor.
  // v7.58.0 -- positionEarningsWatcherCard removed (vestigial: card HTML deleted in this PR).
  // v8.3.1 -- defense-in-depth localStorage cache for v10.or_windows.
  // Why: a Railway redeploy mid-RTH clears the engine's in-memory OR
  // windows; the v8.3.0 backfill rebuilds them within one scan cycle
  // (~60s) but during that gap /api/state returns empty or_windows
  // and the dashboard's OR rows render blank, then "come back" when
  // backfill completes. Cache today's locked OR snapshot in
  // localStorage so the UI keeps showing the last-known-good data
  // through that gap. Engine-side persistence (v8.3.3) is the
  // authoritative fix; this is the client-side belt-and-suspenders.
  function _v10ORCacheKey(dateIso) {
    return "tg.v10.or_windows." + String(dateIso || "");
  }
  function _v10ORCacheSaveIfNonEmpty(s) {
    try {
      var v10 = (s && s.v10) || {};
      var ws = v10.or_windows || {};
      var d = (v10.day_status && v10.day_status.session_date) || "";
      if (!d) return;
      // Save only if at least one ticker has a real (or_high, or_low)
      // pair -- empty / not-yet-populated payloads must NOT overwrite
      // a good cache.
      var keys = Object.keys(ws);
      var hasReal = false;
      for (var i = 0; i < keys.length; i++) {
        var w = ws[keys[i]] || {};
        if (typeof w.or_high === "number" && typeof w.or_low === "number") {
          hasReal = true; break;
        }
      }
      if (!hasReal) return;
      window.localStorage.setItem(_v10ORCacheKey(d), JSON.stringify(ws));
    } catch (e) { /* localStorage may be disabled; ignore */ }
  }
  function _v10ORCacheRestoreIfEmpty(s) {
    try {
      if (!s || !s.v10) return;
      var v10 = s.v10;
      var ws = v10.or_windows || {};
      var d = (v10.day_status && v10.day_status.session_date) || "";
      if (!d) return;
      // Only restore when live payload has no real OR data.
      var keys = Object.keys(ws);
      var hasReal = false;
      for (var i = 0; i < keys.length; i++) {
        var w = ws[keys[i]] || {};
        if (typeof w.or_high === "number" && typeof w.or_low === "number") {
          hasReal = true; break;
        }
      }
      if (hasReal) return;
      var raw = window.localStorage.getItem(_v10ORCacheKey(d));
      if (!raw) return;
      var cached = JSON.parse(raw);
      if (!cached || typeof cached !== "object") return;
      v10.or_windows = cached;
      v10._or_windows_from_cache = true;  // surfaced on UI as "cached"
    } catch (e) { /* parse / storage failure: render whatever came in */ }
  }

  /* Session timeline bar — shows zone bands + real-time ET cursor.
     Renders into #tg-stl-zones / #tg-stl-cursor / #tg-stl-events.
     Called on every state poll; cursor moves to current ET time. */
  function renderSessionBar(s) {
    var track  = document.getElementById("tg-stl-track");
    var zones  = document.getElementById("tg-stl-zones");
    var events = document.getElementById("tg-stl-events");
    var cursor = document.getElementById("tg-stl-cursor");
    if (!track || !zones) return;

    var START = 570, SPAN = 390; /* 9:30=570 .. 16:00=960 */
    function pct(etMin) { return Math.max(0, Math.min(100, (etMin - START) / SPAN * 100)); }

    /* Draw zones once (idempotent via innerHTML guard) */
    if (!zones.__drawn) {
      zones.__drawn = true;
      var ZONE_DEF = [
        [570, 600, "rgba(120,53,15,0.65)",  "OR"],
        [600, 660, "rgba(6,78,59,0.65)",    "ACTIVE"],
        [660, 900, "rgba(15,23,42,0.4)",    ""],
        [900, 960, "rgba(76,29,149,0.65)",  "EOD"],
      ];
      var zh = "";
      ZONE_DEF.forEach(function(z) {
        var l = pct(z[0]), w = pct(z[1]) - l;
        zh += '<div style="position:absolute;top:0;bottom:0;left:' + l.toFixed(1) + '%;width:' + w.toFixed(1) + '%;'
            + 'background:' + z[2] + ';pointer-events:none;display:flex;align-items:center;'
            + 'justify-content:flex-end;padding-right:4px">'
            + (z[3] ? '<span style="font-size:7px;font-weight:700;color:rgba(255,255,255,0.4);'
              + 'letter-spacing:.5px">' + z[3] + '</span>' : '')
            + '</div>';
      });
      /* Boundary ticks */
      [600, 660, 900].forEach(function(m) {
        zh += '<div style="position:absolute;top:0;bottom:0;left:' + pct(m).toFixed(1) + '%;'
            + 'width:1px;background:rgba(255,255,255,0.08);pointer-events:none"></div>';
      });
      zones.innerHTML = zh;
    }

    /* Move cursor to current ET time derived from server_time_label */
    if (cursor) {
      var nowMin = null;
      /* Try server_time_label: "Fri May 16 | 10:26:00 ET" */
      var stl = (s && s.server_time_label) || "";
      var tm = stl.match(/([0-9]{2}):([0-9]{2}):[0-9]{2}/);
      if (tm) nowMin = parseInt(tm[1], 10) * 60 + parseInt(tm[2], 10);
      /* Fallback: real wall clock */
      if (nowMin === null) {
        try {
          var now = new Date();
          var etParts = new Intl.DateTimeFormat("en-US", {
            timeZone: "America/New_York", hour12: false,
            hour: "2-digit", minute: "2-digit"
          }).formatToParts(now);
          var hh = 0, mm2 = 0;
          etParts.forEach(function(p) {
            if (p.type === "hour")   hh  = parseInt(p.value, 10);
            if (p.type === "minute") mm2 = parseInt(p.value, 10);
          });
          nowMin = hh * 60 + mm2;
        } catch (_e) { nowMin = 630; }
      }
      cursor.style.left = pct(nowMin).toFixed(1) + "%";
    }
  }

  function renderAll(s) {
    if (!s || !s.ok) return;
    lastSnapshot = s;
    // v8.3.1 -- cache-restore BEFORE any renderer reads v10.or_windows.
    _v10ORCacheRestoreIfEmpty(s);
    _v10ORCacheSaveIfNonEmpty(s);
    // Publish latest state so the executor-tab IIFE can read market-wide
    // widgets (proximity, regime, gates) from the same source as Main.
    try {
      window.__tgLastState = s;
      if (typeof window.__tgOnState === "function") window.__tgOnState(s);
    } catch (e) {}
    const sl = paperSlice(s);
    renderHeader(s);
    renderSessionBar(s);
    renderKPIs(s, sl);
    renderPositions(s, sl);
    renderTrades(s, sl);
    // v5.18.0 — Main tab render order. The standalone Proximity card
    // was retired; live price + distance-to-OR is now folded into the
    // Permit Matrix Price·Distance column. v5.17.0 retired the legacy
    // Tiger Sovereign / Observer / Gates panels (folded into Weather
    // Check + Permit Matrix). The next-scan countdown is the only
    // piece of the old gates renderer that survived.
    renderNextScanCountdown(s);
    // v7.20.0 — v10 ORB Day Status banner. Renders s.v10 (config +
    // day_status + risk_books). Defensive: never breaks Main if v10
    // block is absent or runtime is unavailable.
    // v7.48.0 -- these renderers physically live in IIFE 2 (where the
    // executor render path was added). Calling them as bare identifiers
    // from IIFE 1 threw ReferenceError silently — same hidden-bug
    // pattern as the v7.44.0 Ticker Matrix fix. Route through the
    // window exports (added in v7.40.0/v7.41.0/v7.45.0) so the Main
    // panel's v10 banner / ticker matrix / activity feed actually
    // render. Falls back to a no-op if exports aren't installed yet
    // (initial page load before IIFE 2 has run).
    try {
      if (typeof window.__tgRenderKillSwitchBanner === "function")
        window.__tgRenderKillSwitchBanner(s, "main");
    } catch (e) { /* never break Main */ }
    try {
      // v7.57.0 -- pidFilter="main" so the banner gauges + strip don't
      // mix val/gene state into the Main tab.
      if (typeof window.__tgRenderV10DayStatus === "function")
        window.__tgRenderV10DayStatus(s, "main");
    } catch (e) { /* never break Main */ }
    try {
      // v7.57.0 -- scope to Main only.
      if (typeof window.__tgRenderV10TickerMatrix === "function")
        window.__tgRenderV10TickerMatrix(s, "main");
    } catch (e) { /* never break Main */ }
    try {
      // v7.57.0 -- scope to Main only.
      if (typeof window.__tgRenderV10ActivityFeed === "function")
        window.__tgRenderV10ActivityFeed(s, "main");
    } catch (e) { /* never break Main */ }
    try {
      if (typeof window.__tgRenderV10ProximityMatrix === "function")
        window.__tgRenderV10ProximityMatrix(s);
    } catch (e) { /* never break Main */ }
    // v7.64.0 -- re-render v10 Backtest Baseline with the cached
    // /api/v10/projection payload so the "Live $X / Δ Y%" pair tracks
    // window.__tgLastState.portfolio.equity in real time instead of
    // waiting 60s for the next projection poll.
    try {
      if (typeof window.__tgRefreshV10Baseline === "function")
        window.__tgRefreshV10Baseline();
    } catch (e) { /* never break Main */ }
    // v7.58.0 -- legacy render calls retired:
    //   renderWeatherCheck, renderPermitMatrix (Tiger Sovereign Phase
    //   1-4 cards; their HTML was removed from index.html)
    //   renderEarningsWatcher, positionEarningsWatcherCard (v6.18.0
    //   EW panel removed; v10 has its own earnings gate)
    // v4.11.0 — health pill bound to Main when active.
    try { applyHealthPill("main", s.errors || { count: 0, severity: "green", entries: [] }); } catch (e) {}
  }

  // v4.1.8-dash — portfolio view toggle removed (Robinhood was
  // deleted in v3.5.0). Only the paper portfolio exists; nothing to
  // wire.

  // ─────── connection management ───────

  let streamConn = null;
  let pollTimer = null;
  let streamTickTimer = null;
  let streamReconnectTimer = null;
  let lastDataAt = 0;

  function setConn(state) {
    const pulse = $("h-pulse");
    if (state === "live") {
      if (pulse) pulse.classList.remove("off");
      $("banner").classList.add("hide");
    } else if (state === "polling") {
      if (pulse) pulse.classList.add("off");
      showBanner("Live stream dropped. Reconnecting… data is polled every 15s.", "warn");
    } else {
      if (pulse) pulse.classList.add("off");
      showBanner("Disconnected from bot. Attempting to reconnect…", "err");
    }
  }
  function showBanner(text, kind) {
    const b = $("banner");
    b.textContent = text;
    b.classList.remove("hide", "banner-ok");
    if (kind === "warn") b.classList.add("banner-ok");
  }

  function startStream() {
    stopStream();
    try {
      streamConn = new EventSource("/stream");
    } catch (e) {
      setConn("down");
      scheduleReconnect();
      return;
    }
    // v9.1.56 — 1s tick refreshes the countdown label using the absolute
    // last_scan_at timestamp stored in window.__lastScanAt. No more
    // per-second decrement needed; remaining is computed from Date.now().
    streamTickTimer = setInterval(updateNextScanLabel, 1000);

    // v9.1.58 -- SSE fires every 5s; only call renderAll when the scan
    // has produced new data (last_scan_at changed). This gives a smooth
    // countdown on every 5s push without the 3x DOM-rebuild rate that
    // caused "sporadic" flicker when SSE was naively at 5s before.
    var _lastRenderedScanAt = null;
    streamConn.addEventListener("state", (ev) => {
      lastDataAt = Date.now();
      setConn("live");
      try {
        var _d = JSON.parse(ev.data).data;
        var _scanAt = (_d && _d.gates && _d.gates.last_scan_at) || null;
        // Always update countdown label (cheap, no DOM rebuild)
        renderNextScanCountdown(_d);
        // Only rebuild the full UI when a new scan has completed
        if (_scanAt !== _lastRenderedScanAt) {
          _lastRenderedScanAt = _scanAt;
          renderAll(_d);
        }
      } catch (e) {}
    });
    streamConn.onerror = () => {
      setConn("polling");
      stopStream();
      startPolling();
      // try to re-open SSE after a delay (guard against multiple scheduled
      // reconnects — the 3s stale-data watchdog can fire back-to-back
      // and queue up duplicates otherwise).
      scheduleStreamReconnect(15000);
    };
  }
  function stopStream() {
    if (streamConn) { try { streamConn.close(); } catch (e) {} streamConn = null; }
    if (streamTickTimer) { clearInterval(streamTickTimer); streamTickTimer = null; }
  }
  function scheduleStreamReconnect(delay) {
    if (streamReconnectTimer) return;
    streamReconnectTimer = setTimeout(() => {
      streamReconnectTimer = null;
      startStream();
    }, delay);
  }

  async function pollOnce() {
    try {
      const r = await fetch("/api/state", { credentials: "same-origin" });
      if (r.status === 401) { location.reload(); return; }
      const s = await r.json();
      lastDataAt = Date.now();
      renderAll(s);
    } catch (e) {
      setConn("down");
    }
  }
  function startPolling() {
    if (pollTimer) return;
    pollOnce();
    pollTimer = setInterval(pollOnce, 15000);
  }
  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }
  function scheduleReconnect() {
    scheduleStreamReconnect(4000);
  }

  // v4.2.2 — client-side 1Hz clock tick. Renders HH:MM:SS + tz
  // label (e.g. "13:09:13 ET") in the row-2 clock.
  // v7.89.0 -- pinned to US/Eastern (was browser-local before).
  // The market clock is ET-based on every other surface (Day Status
  // banner, v10 schedule, archive bar timestamps); the brand clock
  // now matches. The tz token still comes from server_time_label
  // (cached in window.__tgClockTz) and defaults to "ET" when we
  // haven't received a label yet.
  window.__tgTickClock = function () {
    const el = document.getElementById("tg-brand-clock");
    if (!el) return;
    let hh = "--", mm = "--", ss = "--";
    try {
      const parts = new Intl.DateTimeFormat("en-US", {
        timeZone: "America/New_York",
        hour: "2-digit", minute: "2-digit", second: "2-digit",
        hour12: false,
      }).formatToParts(new Date());
      for (const p of parts) {
        if (p.type === "hour") hh = p.value === "24" ? "00" : p.value.padStart(2, "0");
        else if (p.type === "minute") mm = p.value.padStart(2, "0");
        else if (p.type === "second") ss = p.value.padStart(2, "0");
      }
    } catch (e) {
      const d = new Date();
      hh = String(d.getHours()).padStart(2, "0");
      mm = String(d.getMinutes()).padStart(2, "0");
      ss = String(d.getSeconds()).padStart(2, "0");
    }
    const tz = window.__tgClockTz || "ET";
    // v4.3.1 — drop seconds on very narrow phones (<=360px) so
    // the HH:MM TZ label fits inline with logo/version/LIVE pill.
    // v6.0.7 — extend to <=480px (covers iPhone 13/14/15 standard
    // 390 AND iPhone Pro Max 430). At 430 the brand row overflowed
    // by ~70px (body scrollWidth=500, viewport=430) which pushed
    // every page card to the right and clipped Today's Trades and
    // the Permit Matrix STRIKES/State columns. Dropping the :SS
    // segment recovers ~21px and brings the row inside the viewport.
    // v6.11.12 — also drop the "ET" tz suffix on narrow phones.
    // After v6.11.10/11 added the TO pill (bigger now) and brand
    // version string, even "HH:MM ET" plus the TO chip pushed past
    // the viewport. The clock context is implied by the dashboard
    // (US-only) so the suffix is decorative on mobile.
    const narrow = window.matchMedia && window.matchMedia("(max-width: 480px)").matches;
    const t = narrow ? `${hh}:${mm}` : `${hh}:${mm}:${ss}`;
    el.textContent = (tz && !narrow) ? `${t} ${tz}` : t;
  };
  setInterval(window.__tgTickClock, 1000);
  window.__tgTickClock();

  // stale-data watchdog: if no data in 35s (> 2× the 15s SSE push
  // interval), drop to polling. Threshold was 10s before v9.1.49 which
  // caused false triggers on nearly every cycle and produced the
  // "connection dropped" banner during normal operation.
  setInterval(() => {
    if (lastDataAt && (Date.now() - lastDataAt) > 35000 && streamConn) {
      setConn("polling");
      stopStream();
      startPolling();
      scheduleStreamReconnect(15000);
    }
  }, 5000);

  // kick off
  startStream();
  // also fire one immediate poll so the UI populates fast even before first SSE tick
  pollOnce();

  // v5.18.1 \u2014 expose Permit Matrix + Weather Check renderers so the
  // Val/Gene exec IIFE (separate closure below) can mount the same
  // widgets inside its panel skeletons. Both functions accept (s, panel)
  // \u2014 when panel is null they look up by id (Main DOM); when panel
  // is the exec panel root they query [data-f="..."] inside it.
  if (typeof window !== "undefined") {
    // v7.58.0 -- renderWeatherCheck/renderPermitMatrix exports removed
    // with the rest of the legacy Tiger Sovereign UI in this PR.
    // v7.42.0 -- expose renderPositions for smoke render + future tests.
    window.__tgRenderPositions = renderPositions;
    // (v7.40.0 kill-switch banner export lives in the next IIFE
    // where renderKillSwitchBanner is defined.)
    // v5.31.4 — expose Session color helper to the per-executor IIFE
    // below. Defined inside this IIFE; without the bridge it's
    // unreachable from the second IIFE (where Val/Gene tabs render),
    // and renderExecutor throws "__tgSessionColor is not defined" —
    // surfacing as the misleading "Fetch failed: ..." banner because
    // the exception is caught by pollExecutor's try/catch around the
    // fetch. Symptom: open positions don't render on Val/Gene tabs.
    window.__tgSessionColor = __tgSessionColor;
  }
})();

(() => {
  // v4.0.0-beta — tab switcher, index strip, and per-executor polling.
  // All vanilla JS. Independent from the main-tab IIFE above so nothing
  // the main tab does can interfere.
  //
  // v4.11.0 — health-pill helper bridge.
  const applyHealthPill = (typeof window !== "undefined" && typeof window.__tgApplyHealthPill === "function")
    ? window.__tgApplyHealthPill
    : function () { /* no-op fallback */ };

  function $$(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s ?? "").replace(/[&<>"]/g, c =>
      ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;" }[c])
    );
  }
  // v4.0.4 — unify executor KPI formatter with Main's. Previously this
  // returned "+$0.00" (or worse: a locale-dependent bare "+" when the
  // Intl currency option was unsupported) which rendered as a literal
  // "+" placeholder on Val/Gene KPIs. Now mirrors Main's fmtUsd: "$" or
  // "−$" prefix, no surprise "+".
  function fmtUsd(n, digits) {
    if (n === null || n === undefined || isNaN(n)) return "\u2014";
    const v = Number(n);
    const d = (digits === undefined || digits === null) ? 2 : digits;
    const abs = Math.abs(v).toLocaleString("en-US", {
      minimumFractionDigits: d, maximumFractionDigits: d,
    });
    return (v < 0 ? "\u2212$" : "$") + abs;
  }
  function fmtNum(n, d) {
    if (n === null || n === undefined || isNaN(n)) return "\u2014";
    return Number(n).toLocaleString(undefined, {
      minimumFractionDigits: d, maximumFractionDigits: d,
    });
  }

  // --- Tab switching ---------------------------------------------------
  const TABS = ["main", "val", "gene", "lifecycle"];
  let activeTab = "main";

  // v4.1.4-dash — H2: one-shot /api/state warmup when user lands on
  // Val/Gene before Main has populated window.__tgLastState. Without
  // this, shared KPIs (Gate/Regime/Session) render as "—" for up to
  // 15s until the executor poll + Main SSE tick both land. Guarded so
  // we only fire once per tab-switch event, and never blocks the
  // executor poll — both run in parallel.
  let __tgWarmupInFlight = false;
  async function warmupSharedState() {
    if (__tgWarmupInFlight) return;
    if (window.__tgLastState) return;
    __tgWarmupInFlight = true;
    try {
      const r = await fetch("/api/state", { credentials: "same-origin" });
      if (!r.ok) return;
      const s = await r.json();
      window.__tgLastState = s;
      if (typeof window.__tgOnState === "function") window.__tgOnState(s);
    } catch (e) {
      // swallow — next Main SSE/poll tick will populate
    } finally {
      __tgWarmupInFlight = false;
    }
  }

  // v5.19.3 — persist the user's tab choice across page reloads and
  // container redeploys via localStorage. Without this, every redeploy
  // (which forces a fresh fetch) snaps the user back to Main even when
  // they had Val or Gene open. Storage key is namespaced; we ignore
  // localStorage failures (private browsing, disabled storage) and just
  // fall through to the default-Main behavior.
  const TG_TAB_KEY = "tg-active-tab";
  function _tgSaveActiveTab(name) {
    try { window.localStorage.setItem(TG_TAB_KEY, name); } catch (e) { /* ignore */ }
  }
  function _tgLoadActiveTab() {
    try {
      const v = window.localStorage.getItem(TG_TAB_KEY);
      if (v && TABS.includes(v)) return v;
    } catch (e) { /* ignore */ }
    return null;
  }

  function selectTab(name) {
    if (!TABS.includes(name)) return;
    activeTab = name;
    _tgSaveActiveTab(name);
    document.body.setAttribute("data-tg-active-tab", name);
    for (const t of TABS) {
      const panel = $$("tg-panel-" + t);
      if (panel) panel.style.display = (t === name) ? "" : "none";
    }
    for (const btn of document.querySelectorAll(".tg-tab")) {
      const on = btn.getAttribute("data-tg-tab") === name;
      btn.classList.toggle("tg-tab-on", on);
      btn.style.color = on ? "#e7ecf3" : "#8a96a7";
      btn.style.borderBottomColor = on ? "#7dd3fc" : "transparent";
    }
    if (name === "main") {
      // Re-paint pill from cached Main state when switching back.
      const s = window.__tgLastState;
      if (s && typeof applyHealthPill === "function") {
        applyHealthPill("main", s.errors || { count: 0, severity: "green", entries: [] });
      }
    }
    if (name === "val" || name === "gene") {
      if (!window.__tgLastState) warmupSharedState();
      pollExecutor(name);
    }
    if (name === "lifecycle") {
      if (typeof window.__tgLifecycleActivate === "function") {
        window.__tgLifecycleActivate();
      }
    }
  }

  for (const btn of document.querySelectorAll(".tg-tab")) {
    btn.addEventListener("click", () => selectTab(btn.getAttribute("data-tg-tab")));
  }

  // v5.19.3 — restore the previously selected tab on boot. Runs after
  // the click handlers are wired so the panels and tab chrome get the
  // proper visibility/highlight state via the same selectTab path.
  // Defaults to Main if storage is empty or unreadable, matching prior
  // behavior. Lifecycle / Val / Gene activation hooks fire from inside
  // selectTab so deep-linking back to those tabs after a redeploy gets
  // a clean polling/render pass without an extra refresh.
  const __tgInitialTab = _tgLoadActiveTab();
  if (__tgInitialTab && __tgInitialTab !== "main") {
    selectTab(__tgInitialTab);
  }

  // --- Index strip -----------------------------------------------------
  async function pollIndices() {
    try {
      const r = await fetch("/api/indices", { credentials: "same-origin" });
      if (!r.ok) throw new Error("http " + r.status);
      const data = await r.json();
      renderIndices(data);
    } catch (e) {
      const strip = $$("idx-strip");
      if (strip) strip.innerHTML = '<span style="color:#5b6572">indices unavailable</span>';
    }
  }
  // v4.10.0 — cache last indices payload so a viewport resize can
  // re-render the strip in compact-or-full mode without a refetch.
  let __idxLastData = null;
  function renderIndices(data) {
    if (data) __idxLastData = data;
    const strip = $$("idx-strip");
    if (!strip) return;
    const rows = (data && data.indices) || [];
    const session = (data && data.session) || "rth";
    // v4.10.0 — compact mode toggle: ≤640px hides the absolute Δ$ value
    // so 5 items fit horizontally on a 390px phone.
    const compact = (typeof window !== "undefined") && (window.innerWidth <= 640);
    strip.classList.toggle("idx-compact", compact);
    if (!rows.length) {
      strip.innerHTML = '<span style="color:#5b6572">indices unavailable</span>';
      strip.classList.remove("idx-marquee", "is-paused");
      return;
    }
    // v9.1.32 -- suppress the Alpaca VIX row (symbol="VIX", available=false)
    // when a Yahoo ^VIX row is already present and available. Avoids the
    // double-VIX confusion: "VIX: n/a" and "VIX 17.87" both showing.
    const _hasYahooVix = rows.some(r => r.symbol === "^VIX" && r.available && r.last != null);
    const filteredRows = _hasYahooVix
      ? rows.filter(r => !(r.symbol === "VIX" && !r.available))
      : rows;
    const parts = filteredRows.map(r => {
      // v4.13.0 — cash-index rows from Yahoo carry display_label
      // ("S&P 500" instead of "^GSPC") and may include an inline future
      // badge. ETF rows from Alpaca don't have those keys, so we just
      // fall back to the bare symbol like before.
      const labelText = r.display_label || r.symbol;
      if (!r.available || r.last === null || r.last === undefined) {
        return `<span class="idx-item" style="padding:0 14px;border-right:1px solid #1f2937;color:#5b6572">${esc(labelText)}: n/a</span>`;
      }
      const up = (r.change ?? 0) >= 0;
      const color = up ? "#34d399" : "#f87171";
      const sign = up ? "+" : "";
      const chg = (r.change === null || r.change === undefined) ? "—" : sign + fmtNum(r.change, 2);
      const pct = (r.change_pct === null || r.change_pct === undefined) ? "—" : sign + fmtNum(r.change_pct, 2) + "%";
      // AH badge: only shown outside RTH. During RTH the cash market is live
      // so pre/after-hours deltas are redundant and add visual noise.
      let ahHtml = "";
      const isRth = session === "rth";
      if (!isRth && r.ah && r.ah_change !== null && r.ah_change !== undefined) {
        const ahUp = r.ah_change >= 0;
        const ahColor = ahUp ? "#34d399" : "#f87171";
        const ahSign = ahUp ? "+" : "";
        const ahChg = ahSign + fmtNum(r.ah_change, 2);
        const ahPct = (r.ah_change_pct === null || r.ah_change_pct === undefined)
          ? "" : ` ${ahSign}${fmtNum(r.ah_change_pct, 2)}%`;
        const sessLabel = session === "pre" ? "PRE" : "AH";
        ahHtml = ` <span class="idx-ah" title="After-hours move vs close">${sessLabel} <span style="color:${ahColor};font-weight:500">${ahChg}${ahPct}</span></span>`;
      }
      // Futures badge: only shown outside RTH. When the cash market is open
      // the live cash price already reflects futures; the badge is redundant.
      let futHtml = "";
      if (!isRth && r.future && r.future.change_pct !== null && r.future.change_pct !== undefined) {
        const fUp = r.future.change_pct >= 0;
        const fColor = fUp ? "#34d399" : "#f87171";
        const fSign = fUp ? "+" : "";
        const fLabel = esc(r.future.label || r.future.symbol || "FUT");
        const fPct = fSign + fmtNum(r.future.change_pct, 2) + "%";
        futHtml = ` <span class="idx-ah" title="Front-month future vs prior close">[${fLabel} <span style="color:${fColor};font-weight:500">${fPct}</span>]</span>`;
      }
      return `<span class="idx-item" style="padding:0 14px;border-right:1px solid #1f2937"><strong class="idx-sym" style="color:#e7ecf3">${esc(labelText)}</strong> <span class="idx-px" style="color:#8a96a7">${fmtNum(r.last, 2)}</span> <span class="idx-chg" style="color:${color}">${chg}</span> <span class="idx-pct" style="color:${color};font-size:10.5px">${pct}</span>${ahHtml}${futHtml}</span>`;
    });
    // v4.13.0 — if Yahoo failed entirely we keep the ETF rows above
    // (degrade-don't-disappear) and prepend a dim 'data delayed' marker
    // so Val knows the cash/futures view is stale. yahoo_ok is undefined
    // for older payloads or early-Alpaca-failure paths — only paint
    // the marker on an explicit false.
    if (data && data.yahoo_ok === false) {
      parts.unshift('<span class="idx-item" style="padding:0 14px;border-right:1px solid #1f2937;color:#5b6572" title="Yahoo data unavailable; ETFs only">data delayed</span>');
    }
    // v4.12.0 — wrap the items in a single .idx-track. After insertion we
    // measure scrollWidth-vs-clientWidth: if the items overflow we set
    // .idx-marquee on the strip AND duplicate the track innerHTML so the
    // CSS animation (translateX 0 → -50%) loops seamlessly. If everything
    // fits, we leave .idx-marquee off and the strip behaves like before.
    strip.innerHTML = `<div class="idx-track">${parts.join("")}</div>`;
    requestAnimationFrame(() => {
      const track = strip.querySelector(".idx-track");
      if (!track) return;
      strip.classList.remove("idx-marquee", "is-paused");
      // Reset any stale duplicate from a prior render so the measurement
      // below reflects the SINGLE-copy width.
      const overflow = track.scrollWidth > strip.clientWidth + 2;
      if (overflow) {
        track.innerHTML = parts.join("") + parts.join("");
        strip.classList.add("idx-marquee");
      }
    });
  }
  // v4.12.0 — tap-to-pause for touch devices (where :hover doesn't
  // fire). Toggling .is-paused on the strip parks the animation; a
  // second tap resumes. Wired once at init; we use 'click' (works for
  // both mouse and touch) and ignore the event when there's no
  // marquee class in the first place.
  (function wireIdxStripPause() {
    const strip = $$("idx-strip");
    if (!strip) return;
    strip.addEventListener("click", () => {
      if (!strip.classList.contains("idx-marquee")) return;
      strip.classList.toggle("is-paused");
    });
  })();
  // v4.10.0 — debounced re-render on resize so portrait↔landscape recovers
  // the right layout (compact ↔ full) without waiting for the 30s poll.
  let __idxResizeT = null;
  if (typeof window !== "undefined") {
    window.addEventListener("resize", () => {
      if (__idxResizeT) clearTimeout(__idxResizeT);
      __idxResizeT = setTimeout(() => { renderIndices(__idxLastData); }, 150);
    });
  }

  // --- Per-executor tab ------------------------------------------------
  // Render a full Main-style dashboard layout for Val and Gene by cloning
  // the Main tab's widget skeleton (KPIs, grid-2 cards, etc.) and filling
  // only the fields the per-executor API actually exposes. Widgets whose
  // data is main-bot-only (proximity, regime shield, gates, observer,
  // trades table, log tail) stay in the layout as dim placeholders so
  // the visual grid is identical across tabs.

  function escapeExec(s) { return esc(s); }

  // Build the skeleton HTML once per panel. `exec` is "val" or "gene" and
  // is used to scope DOM lookups below (we query within the panel root).
  function execSkeleton(exec) {
    const label = exec === "val" ? "Val" : "Gene";
    return `
<div class="app">

  <section class="killswitch-banner hide" data-f="ks-banner"
           role="alert" aria-live="polite"></section>

  <main class="main">

    <div class="banner hide" data-f="banner"></div>

    <!-- v7.89.0 -- KPI row now ABOVE Open Positions on Val/Gene
         tabs. Operator wants the equity / Day P&L / Open / Session
         summary at the top of the panel so it's visible without
         scrolling, then the positions table beneath it. The
         duplicate port-strip block (Equity / Buying power / Cash
         / Invested / Shorted) that used to sit inside the Open
         Positions card is retired in this version: it repeated
         data already shown in the KPI row above (Equity) and in
         the positions table (Notional column added in v7.89.0). -->
    <section class="kpi-row kpi-row-4">
      <div class="kpi"><span class="kpi-label">Equity</span><span class="kpi-value" data-f="k-equity">—</span><span class="kpi-sub" data-f="k-equity-sub">—</span></div>
      <div class="kpi"><span class="kpi-label">Day P&amp;L</span><span class="kpi-value" data-f="k-pnl">—</span><span class="kpi-sub" data-f="k-pnl-sub">—</span></div>
      <div class="kpi"><span class="kpi-label">Open</span><span class="kpi-value" data-f="k-open">—</span><span class="kpi-sub" data-f="k-open-sub">—</span></div>
      <div class="kpi"><span class="kpi-label">Session</span><span class="kpi-value" data-f="k-session" style="font-size:20px">—</span><span class="kpi-sub" data-f="k-session-sub">—</span></div>
    </section>

    <section class="grid">
      <div class="card">
        <div class="card-head"><span class="card-title">Open positions<span class="count" data-f="pos-count">\u00b7 0</span></span></div>
        <div class="card-body flush" data-f="pos-body">
          <div class="empty">No open positions.</div>
        </div>
      </div>
    </section>

    <!-- v7.47.0 -- per-portfolio v10 strip. Shows THIS portfolio's
         trades / risk / daily-kill gauges + a filtered activity feed.
         Renderer is renderV10PerPortfolio(name, state, panel) in
         renderExecutor; reads from window.__tgLastState (the most
         recent main /api/state snapshot). -->
    <section class="grid" data-f="v10-pid-section">
      <div class="card">
        <div class="card-head">
          <span class="card-title">v10 ORB &middot; ${label}<span class="count" data-f="v10-pid-count">—</span></span>
          <span class="chip" data-f="v10-pid-summary">—</span>
        </div>
        <div class="card-body flush" data-f="v10-pid-body">
          <div class="empty">Waiting for v10 session start...</div>
        </div>
      </div>
    </section>

    <!-- v9.1.0 -- EOD Reversal addon card on Val/Gene tabs (mirrors
         the Main panel v10-eod-section). Populated by
         renderV10EodReversal with pidFilter=${exec}. Per CLAUDE.md
         cross-tab parity rule. -->
    <section class="grid" data-f="v10-eod-section">
      <div class="card">
        <div class="card-head">
          <span class="card-title" title="v9.1.0 EOD Reversal addon. Fires 15:30 ET, flattens 15:59 ET. R17 backtest validated.">EOD Reversal &middot; ${label}</span>
          <span class="chip" data-f="v10-eod-pid-status">&mdash;</span>
          <span class="chip" data-f="v10-eod-pid-fire" style="background:rgba(245,158,11,0.18);color:#f59e0b">paper</span>
        </div>
        <div class="card-body flush" data-f="v10-eod-pid-body" style="padding:10px 14px;font-family:'JetBrains Mono',monospace;font-size:12px;color:#e5e7eb">
          <div style="color:#6b7280">No EOD activity yet today.</div>
        </div>
      </div>
    </section>

    <!-- v8.3.21 -- Proximity moved ABOVE Recent activity so Val/Gene
         section order matches Main (Day Status -> Ticker Matrix ->
         Baseline -> Proximity -> Activity -> Trades). New CLAUDE.md
         rule: section order parity across all three tabs. -->
    <!-- v7.55.0 -- v10 Proximity card on Val/Gene tabs (mirrors the
         Main panel card from v7.52.0). Same renderer, same scope (the
         v10 universe is market-wide), but the per-pid phase chips
         filter to this portfolio. Click any row to expand the intraday
         chart with OR overlays + entry/exit markers. -->
    <section class="grid" data-f="v10-prox-section-pid">
      <div class="card">
        <div class="card-head">
          <span class="card-title" title="Distance from current price to OR break levels + per-(pid,ticker) FSM phase + trades n/cap. Click any row to expand the intraday chart.">OR Proximity &middot; ${label}<span class="count" data-f="v10-prox-pid-count">\u2014</span></span>
          <span class="chip" data-f="v10-prox-pid-summary">\u2014</span>
        </div>
        <div class="card-body flush" data-f="v10-prox-pid-body">
          <div class="empty">Waiting for v10 universe...</div>
        </div>
      </div>
    </section>

    <section class="grid" data-f="v10-pid-activity-section">
      <div class="card">
        <div class="card-head">
          <span class="card-title">Recent activity &middot; ${label}<span class="count" data-f="v10-pid-act-count">—</span></span>
          <span class="chip" data-f="v10-pid-act-summary">—</span>
        </div>
        <div class="card-body flush" data-f="v10-pid-act-body">
          <div class="empty">No v10 events on this portfolio yet today.</div>
        </div>
      </div>
    </section>

    <!-- v7.58.0 -- Weather Check banner + Permit Matrix card removed
         from the exec skeleton too (mirrors the Main panel cleanup
         in this PR). v10 ORB surfaces above carry the live decision
         path; legacy Tiger Sovereign cards added no signal. -->

    <section class="grid">
      <div class="card">
        <div class="card-head">
          <span class="card-title" title="All fills (opens + closes) recorded today, newest first">Today's trades<span class="count" data-f="trades-count">\u00b7 \u2014</span></span>
          <span class="chip" data-f="trades-realized">\u2014</span>
        </div>
        <!-- v7.0.3 \u2014 one-line daily summary parity with Main. -->
        <div class="trades-summary" data-f="trades-summary">\u2014</div>
        <div class="card-body flush" data-f="trades-body">
          <div class="empty">No trades today.</div>
        </div>
      </div>
    </section>

    <section>
      <div class="card">
        <div class="card-head"><span class="card-title">Account diagnostics</span></div>
        <div class="card-body">
          <div class="key-val-grid">
            <dt>Account number</dt><dd data-f="d-account" style="text-align:right">\u2014</dd>
            <dt>Status</dt><dd data-f="d-status" style="text-align:right">\u2014</dd>
            <dt>Alpaca base URL</dt><dd data-f="d-baseurl" style="text-align:right">\u2014</dd>
            <dt>Last error</dt><dd data-f="d-error" style="text-align:right">\u2014</dd>
          </div>
        </div>
      </div>
    </section>

  </main>
</div>`;
  }

  // Inject the skeleton once; store a `data-tg-ready` flag on the panel
  // so subsequent polls just update fields instead of re-parsing HTML.
  function ensureExecSkeleton(exec) {
    const panel = $$("tg-panel-" + exec);
    if (!panel) return null;
    if (!panel.dataset.tgReady) {
      panel.innerHTML = execSkeleton(exec);
      panel.dataset.tgReady = "1";
      // v9.1.33 -- mark the panel as loading so KPI values shimmer
      // until the first successful poll fills them in.
      panel.classList.add("tg-exec-loading");
    }
    return panel;
  }

  // v7.47.0 -- per-portfolio v10 sections. Renders THIS portfolio's
  // trades / concurrent-risk / daily-kill gauges + a pid-filtered
  // activity feed into the exec skeleton. Reads from
  // window.__tgLastState (the most recent Main /api/state snapshot)
  // since the /api/executor/<name> endpoint does not carry v10 state.
  // Section anchors live in execSkeleton under data-f="v10-pid-*".
  //
  // v7.50.0 -- the third arg `execData` is the /api/executor/<name>
  // payload. Used to source `trades_today` from the broker side
  // instead of the v10 FSM RiskBook. Reason: when
  // ORB_PORTFOLIO_FIRE=0 (default until the 5-day paper-fire
  // observation lands) Val/Gene mirror Main via the legacy signal
  // bus -- their v10 RiskBook trades_today stays at 0 because the
  // fire/exit doesn't route through the v10 FSM for those pids. The
  // broker-reported count is correct in BOTH modes (mirror and
  // standalone-fire) and is what the operator actually wants to see.
  function renderV10PerPortfolio(name, panel, execData) {
    function esc(v) {
      return String(v == null ? "" : v)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;")
        .replace(/>/g, "&gt;").replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
    }
    var s = window.__tgLastState;
    var v10 = s && s.v10;
    var pid = name; // "val" or "gene" maps directly to portfolio_id
    var section = execField(panel, "v10-pid-section");
    var actSection = execField(panel, "v10-pid-activity-section");
    var proxSection = execField(panel, "v10-prox-section-pid");

    // v7.55.0 -- proximity card stays visible across all states
    // (matches Main panel behaviour from v7.54.0). The other v10
    // sections still hide when v10 isn't bootstrapped, but proximity
    // can still surface the universe + current prices pre-bootstrap.
    if (s && s.v10) {
      try { renderV10ProximityForPanel(s, panel, pid); }
      catch (e) { /* never break exec render */ }
      // v9.1.0 -- EOD reversal addon card.
      try { renderV10EodReversal(s, pid, panel); }
      catch (e) { /* never break exec render */ }
    } else if (proxSection) {
      proxSection.style.display = "none";
    }

    if (!v10 || v10.available === false || !v10.bootstrapped) {
      if (section) section.style.display = "none";
      if (actSection) actSection.style.display = "none";
      return;
    }
    if (section) section.style.display = "";
    if (actSection) actSection.style.display = "";

    var rb = (v10.risk_books || {})[pid] || {};
    var dayStates = v10.day_states || [];
    var myDayState = null;
    for (var i = 0; i < dayStates.length; i++) {
      if ((dayStates[i].portfolio_id || "").toLowerCase() === pid) {
        myDayState = dayStates[i];
        break;
      }
    }
    // v7.50.0 -- broker-reported trade count is the source of truth
    // for the UI. Three sources, preferred in order:
    //   1. exec payload trades_today.length (live broker count)
    //   2. backend-injected day_state.broker_trades_today (from
    //      dashboard_server, falls back if exec poll hasn't landed)
    //   3. v10 RiskBook trades_today (correct only when
    //      ORB_PORTFOLIO_FIRE=1; stays at 0 in mirror mode)
    var brokerTrades = (execData && Array.isArray(execData.trades_today))
                        ? execData.trades_today.length
                        : (myDayState && myDayState.broker_trades_today != null
                            ? myDayState.broker_trades_today
                            : null);
    var v10Trades = (myDayState && myDayState.trades_today) || 0;
    var trades = (brokerTrades != null) ? brokerTrades : v10Trades;
    var maxTrades = (v10.config && v10.config.max_trades_per_day) || 5;
    // v7.63.0 -- compute per-ticker top count for this pid so the
    // Val/Gene Trades gauge matches Main's "X total · top ticker N/5"
    // framing (v7.57.0). Per-ticker counts come from execData.trades_today
    // (a list of trade dicts from the broker's trade_history); fall back
    // to v10 day_states which only have per-(pid,ticker) FSM counts.
    var perTickerCount = {};
    if (execData && Array.isArray(execData.trades_today)) {
      execData.trades_today.forEach(function (t) {
        var tk = t && t.ticker;
        if (!tk) return;
        perTickerCount[tk] = (perTickerCount[tk] || 0) + 1;
      });
    }
    // Augment with v10 FSM trades_today (covers the ORB_PORTFOLIO_FIRE=1
    // case where v10 fired directly).
    for (var k = 0; k < dayStates.length; k++) {
      var d2 = dayStates[k];
      if ((d2.portfolio_id || "").toLowerCase() !== pid) continue;
      if (!d2.ticker) continue;
      var n = d2.trades_today || 0;
      if (n > (perTickerCount[d2.ticker] || 0)) perTickerCount[d2.ticker] = n;
    }
    var topTicker = 0;
    Object.keys(perTickerCount).forEach(function (tk) {
      if (perTickerCount[tk] > topTicker) topTicker = perTickerCount[tk];
    });

    var openRisk = rb.open_risk || 0;
    var maxRisk = rb.max_risk_dollars || 0;
    var openCount = rb.open_count || 0;
    var ut = rb.utilization_pct || 0;
    var killThr = rb.daily_kill_threshold || 0;
    var realizedToday = rb.realized_pnl_today || 0;
    var killTriggered = !!rb.daily_kill_triggered;
    var killPct = (killThr > 0 && realizedToday < 0)
      ? Math.abs(realizedToday) / killThr * 100 : 0;

    // v7.63.0 -- gauge fill follows top-ticker / cap (the actually-
    // binding constraint), matching Main's framing.
    var tradesPct = maxTrades > 0 ? (topTicker / maxTrades * 100) : 0;
    var riskPct = maxRisk > 0 ? (openRisk / maxRisk * 100) : 0;

    var countEl = execField(panel, "v10-pid-count");
    if (countEl) countEl.textContent = "· " + trades + " today";
    var summaryEl = execField(panel, "v10-pid-summary");
    if (summaryEl) {
      if (killTriggered) {
        summaryEl.textContent = "DAILY KILL TRIPPED";
        summaryEl.style.background = "#dc2626";
        summaryEl.style.color = "#fff";
      } else if (killPct >= 70 || tradesPct >= 70 || riskPct >= 70) {
        summaryEl.textContent = "near limit";
        summaryEl.style.background = "#f59e0b";
        summaryEl.style.color = "#0a0d12";
      } else if (openCount > 0) {
        summaryEl.textContent = openCount + " open · " + ut.toFixed(0) + "% util";
        summaryEl.style.background = "#1f2937";
        summaryEl.style.color = "#cbd5e1";
      } else {
        summaryEl.textContent = "idle";
        summaryEl.style.background = "#1f2937";
        summaryEl.style.color = "#9ca3af";
      }
    }

    function _gaugeHtml(label, value, fillPct, cls) {
      var clamped = Math.max(0, Math.min(110, fillPct));
      var stateCls = '';
      if (fillPct >= 90 || (cls || '').indexOf('danger') >= 0)
        stateCls = ' v10-gauge-danger';
      else if (fillPct >= 70) stateCls = ' v10-gauge-warn';
      return '<div class="v10-gauge ' + (cls || '') + stateCls + '">'
           + '<div class="v10-gauge-head">'
           + '<span class="v10-gauge-label">' + esc(label) + '</span>'
           + '<span class="v10-gauge-value">' + esc(value) + '</span>'
           + '</div>'
           + '<div class="v10-gauge-bar">'
           + '<div class="v10-gauge-fill" style="width:'
                + clamped.toFixed(1) + '%"></div>'
           + '</div></div>';
    }

    var body = execField(panel, "v10-pid-body");
    if (body) {
      var html = '';
      // v7.63.0 -- label + value match Main's v7.57.0 framing so an
      // operator switching tabs sees the same shape: "Trades today
      // (cap 5/ticker)" / "X total · top ticker N/5".
      html += _gaugeHtml(
        'Trades today (cap ' + maxTrades + '/ticker)',
        trades + ' total · top ticker ' + topTicker + '/' + maxTrades,
        tradesPct);
      html += _gaugeHtml('Concurrent risk',
        '$' + Math.round(openRisk).toLocaleString() +
          ' / $' + Math.round(maxRisk).toLocaleString() +
          ' (' + riskPct.toFixed(0) + '%)',
        riskPct);
      if (killThr > 0) {
        var killValue = '$' + Math.round(realizedToday).toLocaleString() +
                         ' / -$' + Math.round(killThr).toLocaleString();
        var killCls = 'v10-gauge-kill' + (killTriggered ? ' danger' : '');
        html += _gaugeHtml('Daily-kill', killValue, killPct, killCls);
      }

      // v9.0.0 -- session-wide chase-prevention + regime-skip chips
      // for parity with Main tab. Compact strip below the gauges.
      var v9cfg = (v10 && v10.config) || {};
      var v9ds = (v10 && v10.day_status) || {};
      var v9chips = [];
      var v9spyThr = parseFloat(v9ds.spy_threshold_bps || 0);
      if (v9spyThr !== 0) {
        var v9spyRet = v9ds.spy_d1_ret_bps;
        var v9spyTxt, v9spyBg, v9spyFg;
        if (v9spyRet == null) {
          v9spyTxt = "SPY n/a"; v9spyBg = "#374151"; v9spyFg = "#e5e7eb";
        } else {
          var v9retPct = (v9spyRet / 100).toFixed(2);
          var v9blocked = v9spyRet < v9spyThr;
          v9spyTxt = "SPY " + (v9spyRet >= 0 ? "+" : "") + v9retPct + "% · "
            + (v9blocked ? "BLOCK" : "PASS");
          v9spyBg = v9blocked ? "rgba(220,38,38,0.18)" : "rgba(22,163,74,0.18)";
          v9spyFg = v9blocked ? "#fca5a5" : "#86efac";
        }
        v9chips.push(
          '<span style="padding:3px 8px;border-radius:999px;font-size:11px;font-weight:600;background:' + v9spyBg + ';color:' + v9spyFg + '" title="Prior-day SPY return regime gate (v9.0.0+). Threshold ' + (v9spyThr/100).toFixed(2) + '%.">'
            + v9spyTxt + '</span>'
        );
      }
      var v9mbrN = parseInt((v10 && v10.mbr_reject_count) || 0, 10) || 0;
      if (v9mbrN > 0) {
        v9chips.push(
          '<span style="padding:3px 8px;border-radius:999px;font-size:11px;font-weight:600;background:rgba(59,130,246,0.18);color:#60a5fa" title="Weak-break entries rejected this session (v9.0.0 mbr filter). Threshold ' + parseFloat(v9cfg.min_break_bps || 0).toFixed(0) + 'bps.">mbr ' + v9mbrN + '</span>'
        );
      }
      var v9chaseN = parseInt((v10 && v10.vwap_chase_reject_count) || 0, 10) || 0;
      if (v9chaseN > 0) {
        var v9fence = (v9cfg.max_vwap_dev_tickers || []);
        v9chips.push(
          '<span style="padding:3px 8px;border-radius:999px;font-size:11px;font-weight:600;background:rgba(168,85,247,0.18);color:#c4b5fd" title="Mega-cap chase-fence rejections (v9.0.0+). Threshold ' + parseFloat(v9cfg.max_vwap_dev_bps || 0).toFixed(0) + 'bps. Fence: ' + (v9fence.length ? v9fence.join(",") : "global") + '.">chase ' + v9chaseN + '</span>'
        );
      }
      var v9chipsHtml = v9chips.length
        ? '<div style="margin-top:8px;display:flex;gap:6px;align-items:center;flex-wrap:wrap"><span style="color:#9ca3af;font-size:11px">Session:</span>' + v9chips.join('') + '</div>'
        : '';

      body.innerHTML = '<div class="v10-gauges-row">' + html + '</div>' + v9chipsHtml;
    }

    // v8.3.16 -- suppress same-tick opposite_side rejects from the
    // executor-tab activity feed. They fire for every 5m candle that
    // straddles both OR bounds; the engine correctly admits one side
    // and rejects the other. These rejects are guard-rail success
    // signals, not actionable failures. Keeping them in the feed
    // drowns out real notional_cap / no_signal / kill events the
    // operator needs to see.
    function _is_noise_reject(ev) {
      if ((ev.kind || "").toLowerCase() !== "reject") return false;
      var d = String(ev.detail || "");
      return d.indexOf("opposite_side:") !== -1;
    }
    var events = (v10.activity || []).filter(function (e) {
      if ((e.pid || "").toLowerCase() !== pid) return false;
      if (_is_noise_reject(e)) return false;
      return true;
    });
    var actCount = execField(panel, "v10-pid-act-count");
    if (actCount) actCount.textContent = "· " + events.length;
    var actSummary = execField(panel, "v10-pid-act-summary");
    if (actSummary) {
      if (events.length === 0) {
        actSummary.textContent = "no events yet";
      } else {
        var first = events[0];
        // v7.82.0 -- display in browser-local timezone (was raw UTC).
        actSummary.textContent = "most recent · " + window.utcIsoToLocalHHMM(first.ts_iso || "");
      }
    }
    var actBody = execField(panel, "v10-pid-act-body");
    if (actBody) {
      if (events.length === 0) {
        // RTH-aware empty state: show "session starts" instead of "no events"
        // so the operator's eye-trace is identical across all three tabs.
        var _actMode2 = ((s.regime || {}).mode || "CLOSED");
        var _actRth2 = (_actMode2 === "OPEN" || _actMode2 === "OR"
          || _actMode2 === "POWER" || _actMode2 === "PRE");
        var _actSession2 = !!((s.v10 || {}).session_date);
        if (!_actRth2 && !_actSession2) {
          actBody.innerHTML = '<div class="empty" style="font-size:11px;padding:10px 14px">'
            + '&mdash; session starts 09:25 ET &mdash;</div>';
        } else {
          actBody.innerHTML = '<div class="empty">No v10 events on this portfolio yet today.</div>';
        }
      } else {
        var rowsHtml = [];
        for (var j = 0; j < events.length; j++) {
          var e = events[j];
          // v8.3.16 -- ET conversion (matches v8.3.1's Main-tab fix).
          // Pre-v8.3.16 this path used a raw ts.split("T")[1].slice(0,5)
          // which renders UTC. The Val tab showed "16:04" while it was
          // really 12:04 ET (during EDT). Route through the shared
          // helper so all activity-feed surfaces agree on the market
          // clock zone.
          var hhmm2 = (typeof window.utcIsoToLocalHHMM === "function")
              ? window.utcIsoToLocalHHMM(e.ts_iso || "")
              : ((e.ts_iso || "").split("T")[1] || "").slice(0, 5);
          if (!hhmm2) hhmm2 = "—";
          var kindCls = "act-kind-" + (e.kind || "info");
          var kindTxt = (e.kind || "info").toUpperCase().replace(/_/g, " ");
          var ticker = e.ticker || "—";
          rowsHtml.push(
            '<div class="act-row">' +
              '<span class="act-time">' + esc(hhmm2) + '</span>' +
              '<span class="act-ticker">' + esc(ticker) + '</span>' +
              '<span class="act-kind ' + kindCls + '">' + esc(kindTxt) + '</span>' +
              '<span class="act-detail">' + esc(e.detail || "") + '</span>' +
            '</div>'
          );
        }
        actBody.innerHTML = '<div>' + rowsHtml.join("") + '</div>';
      }
    }
  }

  function execField(panel, name) {
    return panel.querySelector(`[data-f="${name}"]`);
  }
  function setField(panel, name, value) {
    const el = execField(panel, name);
    if (el) el.textContent = value;
    return el;
  }
  function setFieldHtml(panel, name, html) {
    const el = execField(panel, name);
    if (el) el.innerHTML = html;
    return el;
  }

  // v7.50.0 -- last exec payload cache, keyed by name. The Main
  // /api/state refresh path (window.__tgOnState below) needs to
  // re-render the per-portfolio v10 strip with broker trades_today
  // -- but it doesn't have access to the exec payload directly. We
  // stash the latest one here so the refresh can read it.
  // v7.52.0 -- also exposed on window so the Main per-pid strip in
  // renderV10DayStatus can pull Alpaca-reported equity for val/gene
  // (the v10 RiskBook's equity is stale in mirror mode).
  var _execLastData = {};
  if (typeof window !== "undefined") {
    window.__tgExecLastData = _execLastData;
  }

  async function pollExecutor(name) {
    const panel = ensureExecSkeleton(name);
    if (!panel) return;
    try {
      const r = await fetch("/api/executor/" + name, { credentials: "same-origin" });
      if (!r.ok) throw new Error("http " + r.status);
      const data = await r.json();
      _execLastData[name] = data;
      // v9.1.33 -- clear loading shimmer on first successful data fetch.
      if (panel) panel.classList.remove("tg-exec-loading");
      renderExecutor(name, data);
      // v4.11.0 — paint health pill from per-executor errors snapshot.
      try { applyHealthPill(name, (data && data.errors) || { count: 0, severity: "green", entries: [] }); } catch (e) {}
    } catch (e) {
      const banner = execField(panel, "banner");
      if (banner) {
        banner.classList.remove("hide");
        banner.textContent = "Fetch failed: " + (e.message || e);
      }
    }
  }

  function renderBadge(name, data) {
    // v6.11.9 — simplified to a single ✓ / ✗ mark in the tab heading.
    // v6.11.10 — added L/P mode mark next to the ✓.
    // v6.11.11 — align with Main tab format. Paper -> "📄 Paper"
    // (matches Main); live -> just "✓ L"; disabled stays ✗.
    // renderHeader() also writes this badge from s.executors_status as
    // a faster initial paint; this per-executor poll keeps it accurate
    // for executors that go offline mid-session.
    const el = $$("tg-badge-" + name);
    if (!el) return;
    const label = name.charAt(0).toUpperCase() + name.slice(1);
    if (!data || data.enabled === false) {
      el.innerHTML = "OFF";
      el.style.color = "var(--text-dim)";
      el.setAttribute("title",
        `${label} executor disabled (missing PAPER_KEY or *_ENABLED=0)`);
      // Dim the entire tab button so disabled tabs don't compete visually
      // with enabled ones. The tab is still clickable (shows a banner).
      const tabBtn = el.closest(".tg-tab");
      if (tabBtn) tabBtn.style.opacity = "0.45";
      return;
    }
    // Clear any dim from a previous disabled render
    const _tabBtn = el.closest(".tg-tab");
    if (_tabBtn) _tabBtn.style.opacity = "";
    const mode = (data.mode === "live") ? "live" : "paper";
    const _posN = Array.isArray(data.positions) ? data.positions.length : 0;
    // Publish to window so renderHeader (IIFE-1) can read the same count.
    window.__tgExecPosN = window.__tgExecPosN || {};
    window.__tgExecPosN[name] = _posN;
    const _posTag = _posN > 0
      ? `<span style="color:#fbbf24;font-weight:600;margin-left:5px">${_posN}</span>`
      : "";
    if (mode === "live") {
      el.innerHTML =
        '<span style="color:#22c55e;font-size:8px;vertical-align:middle">&#9679;</span>' +
        '<span style="color:#86efac;font-size:10px;font-weight:500;margin-left:3px">live</span>' +
        _posTag;
    } else {
      el.innerHTML = '\ud83d\udcc4 <span style="color:#5b6572">Paper</span>' + _posTag;
    }
    el.style.color = "";
    el.setAttribute("title", `${label} executor enabled (${mode} mode)` +
      (_posN > 0 ? ` \u00b7 ${_posN} open position${_posN > 1 ? "s" : ""}` : ""));
  }

  // Render helpers shared across Val/Gene. These mirror the formatters
  // used by the Main IIFE above (fmtUsd/fmtPct there use "\u2212" minus
  // signs; we stick with the Intl currency formatter to keep footprint
  // small — visual parity close enough since KPIs are the same cards).
  function fmtPctExec(v, d) {
    if (v === null || v === undefined || isNaN(v)) return "\u2014";
    const abs = Math.abs(v);
    const digits = d ?? (abs < 0.1 ? 3 : 2);
    return (v >= 0 ? "+" : "\u2212") + abs.toFixed(digits) + "%";
  }

  // --- Market-state widgets (Weather Check + Permit Matrix) ---------
  // These are scanner-level signals that are the same for every
  // executor by design, so we render them from the Main /api/state
  // payload (republished on window.__tgLastState) rather than from the
  // per-executor snapshot.
  // v5.17.0 \u2014 dropped Sovereign Regime Shield + Gates\u00b7entry-checks.
  // v5.18.1 \u2014 retired the standalone Proximity card and replaced it
  // with the Weather Check banner + Permit Matrix card from Main, so
  // the per-executor tabs surface the same gate view operators see on
  // Main. Both renderers accept an optional `panel` arg so they read
  // [data-f="..."] hooks inside the exec panel.
  //
  // fmtPxExec is kept for any future per-exec price formatter wiring.

  function fmtPxExec(v) {
    if (v === null || v === undefined || isNaN(v)) return "\u2014";
    return "$" + Number(v).toFixed(2);
  }

  // v5.18.1 \u2014 renderExecProximity + execRenderPermitSideChip removed.
  // The Val/Gene tabs now render the same Weather Check + Permit Matrix
  // as Main (renderWeatherCheck/renderPermitMatrix accept a panel arg).


  // --- Today's trades (per-executor) ---------------------------------
  // Uses executor snapshot's `todays_trades` list (Alpaca filled orders,
  // today ET). Row template mirrors Main's Today's Trades card.
  function fmtTradeTimeExec(rawT) {
    const s = (rawT || "").toString();
    if (!s) return "\u2014";
    const iso = s.match(/^\d{4}-\d{2}-\d{2}T(\d{2}:\d{2})/);
    if (iso) return iso[1];
    const hm = s.match(/^\d{1,2}:\d{2}/);
    return hm ? hm[0] : s;
  }
  // v7.0.3 \u2014 mirrors Main's computeTradesSummary so the per-executor
  // 'opens / closes / realized / win-rate' line is identical to Main's.
  // BUY|SHORT count as opens; SELL|COVER count as closes (closes are the
  // only rows that may carry a pnl number).
  function computeTradesSummaryExec(trades) {
    let opens = 0, closes = 0, wins = 0, realized = 0, have_pnl = 0;
    for (const t of (trades || [])) {
      const act = (t.action || "").toUpperCase();
      const isOpen = (act === "BUY" || act === "SHORT");
      const isClose = (act === "SELL" || act === "COVER");
      if (isOpen) opens += 1;
      else if (isClose) {
        closes += 1;
        if (typeof t.pnl === "number" && isFinite(t.pnl)) {
          realized += t.pnl;
          have_pnl += 1;
          if (t.pnl > 0) wins += 1;
        }
      }
    }
    const win_rate = have_pnl > 0 ? (wins / have_pnl) : null;
    return { opens, closes, wins, realized, have_pnl, win_rate };
  }

  function renderExecTrades(panel, data, disabled) {
    const body = execField(panel, "trades-body");
    const count = execField(panel, "trades-count");
    const chip = execField(panel, "trades-realized");
    const sumEl = execField(panel, "trades-summary");
    const trades = (data && Array.isArray(data.todays_trades)) ? data.todays_trades : [];
    // "0" not "\u2014" when disabled: "\u2014" means unavailable, "0" means confirmed zero.
    if (count) count.textContent = "\u00b7 " + (disabled ? "0" : trades.length);

    // v7.0.3 \u2014 use the same summary calc Main uses so opens/closes
    // counts match and the chip aggregate is identical.
    const summary = computeTradesSummaryExec(trades);
    if (chip) {
      if (disabled || summary.have_pnl === 0) {
        chip.textContent = "\u2014";
        chip.className = "chip chip-neut";
      } else {
        chip.textContent = fmtUsd(summary.realized);
        chip.className = "chip " + (summary.realized > 0 ? "chip-ok" : (summary.realized < 0 ? "chip-down" : "chip-neut"));
      }
    }

    // v7.0.3 \u2014 inline summary line above the table (parity with Main).
    if (sumEl) {
      if (disabled) {
        sumEl.innerHTML = '<span class="ts-seg">\u2014</span>';
      } else if (!trades.length) {
        sumEl.innerHTML = '<span class="ts-seg" title="No buy or sell fills have been recorded today">No fills yet today.</span>';
      } else {
        const realCls = summary.have_pnl === 0 ? "na"
                      : (summary.realized > 0 ? "up" : (summary.realized < 0 ? "down" : ""));
        const realTxt = summary.have_pnl === 0 ? "\u2014" : fmtUsd(summary.realized);
        const wrTxt   = summary.win_rate === null ? "\u2014"
                      : (Math.round(summary.win_rate * 100) + "%");
        sumEl.innerHTML =
          `<span class="ts-seg" title="Number of opening fills today (BUY for long, SHORT for short)"><span class="ts-val">${summary.opens}</span> open${summary.opens===1?"":"s"}</span>` +
          `<span class="ts-seg" title="Number of closing fills today (SELL for long, COVER for short)"><span class="ts-val">${summary.closes}</span> close${summary.closes===1?"":"s"}</span>` +
          `<span class="ts-seg" title="Sum of realized P&L from closed pairs today, after commissions">realized <span class="ts-val ${realCls}">${realTxt}</span></span>` +
          `<span class="ts-seg" title="Win rate among closed pairs today (winners / total closed)">win <span class="ts-val">${wrTxt}</span></span>`;
      }
    }

    if (!body) return;
    if (disabled) {
      body.innerHTML = `<div class="empty">\u2014</div>`;
      return;
    }
    if (!trades.length) {
      body.innerHTML = `<div class="empty">No trades today.</div>`;
      return;
    }
    const rows = trades.map((t) => {
      const tm = fmtTradeTimeExec(t.filled_at || t.time || t.entry_time);
      const act = (t.action || "").toUpperCase();
      // v7.0.3 \u2014 classify by open vs close so SHORT/COVER pairs are
      // colored correctly (previously only BUY/SELL were recognized; a
      // SHORT fill rendered as a buy-style chip and COVER fills with pnl
      // were ignored by the running tail).
      const isOpen = (act === "BUY" || act === "SHORT");
      const isClose = (act === "SELL" || act === "COVER");
      const sym = t.ticker || t.symbol || "\u2014";
      const shares = t.shares ?? t.qty;
      const px = t.price ?? t.avg_fill_price ?? t.entry_price ?? t.exit_price;
      const actCls = isClose ? "act-sell" : "act-buy";
      const actLbl = act || "\u2014";
      let tailHtml = "\u2014";
      if (isOpen) {
        const cost = (typeof t.cost === "number" && isFinite(t.cost))
          ? t.cost
          : ((typeof shares === "number" && typeof px === "number") ? shares * px : null);
        tailHtml = cost !== null
          ? `<span class="trade-cost">${fmtUsd(cost)}</span>`
          : `<span class="trade-cost">\u2014</span>`;
      } else if (isClose) {
        const pnl = (typeof t.pnl === "number" && isFinite(t.pnl)) ? t.pnl : null;
        const pnlPct = (typeof t.pnl_pct === "number" && isFinite(t.pnl_pct)) ? t.pnl_pct : null;
        if (pnl !== null) {
          const pnlCls = pnl > 0 ? "up" : (pnl < 0 ? "down" : "");
          const sign = pnl > 0 ? "+" : "";
          const pctStr = pnlPct !== null ? ` <span class="pnl-pct ${pnlCls}">${fmtPctExec(pnlPct)}</span>` : "";
          tailHtml = `<span class="trade-pnl ${pnlCls}">${sign}${fmtUsd(pnl)}${pctStr}</span>`;
        } else {
          tailHtml = `<span class="trade-pnl">\u2014</span>`;
        }
      }
      return `<div class="trade-row" data-act="${esc(act)}" data-sym="${esc(sym)}">
        <span class="tr-time">${esc(tm)}</span>
        <span class="tr-sym ticker">${esc(sym)}</span>
        <span class="tr-qty">${shares ?? "\u2014"}</span>
        <span class="tr-act"><span class="act-badge ${actCls}">${esc(actLbl)}</span></span>
        <span class="tr-tail">${tailHtml}</span>
        <span class="tr-price">${fmtPxExec(px)}</span>
      </div>`;
    }).join("");
    body.innerHTML = `<div class="trades-list">${rows}</div>`;
  }

  function renderExecMarketState(panel) {
    const s = window.__tgLastState;
    if (!s) return;
    // v5.18.1 \u2014 the standalone Proximity card was replaced with the
    // same Weather Check + Permit Matrix sections shown on Main. Both
    // renderers accept an optional `panel` arg so they read the
    // [data-f="..."] hooks inside this exec panel instead of Main's id
    // hooks. Data is market-wide (window.__tgLastState is Main's last
    // /api/state payload) so the Val/Gene tabs see the exact same
    // gates Main does. Renderers live in the Main IIFE above and are
    // bridged across closures via window.__tgRender{WeatherCheck,PermitMatrix}.
    try { if (typeof window.__tgRenderWeatherCheck === "function") window.__tgRenderWeatherCheck(s, panel); } catch (e) {}
    try { if (typeof window.__tgRenderPermitMatrix === "function") window.__tgRenderPermitMatrix(s, panel); } catch (e) {}
  }

  function renderExecutor(name, data) {
    // v9.1.9 -- cache the latest data so the position-row click
    // handler can re-render via window.__tgRenderExecutor(name, ...)
    // without waiting for the next state poll.
    if (typeof window !== "undefined") {
      window.__tgLastExecData = window.__tgLastExecData || {};
      window.__tgLastExecData[name] = data;
    }
    renderBadge(name, data);
    const panel = ensureExecSkeleton(name);
    if (!panel) return;
    const label = name === "val" ? "Val" : "Gene";
    const disabled = !data || data.enabled === false;

    // v7.40.0 -- kill-switch banner mirrors on Val/Gene from main /api/state
    // so an operator switching tabs sees the same alert. The exec endpoint
    // doesn't carry these flags, so we read from window.__tgLastState
    // (the most recent main /api/state snapshot).
    try {
      var lastMain = window.__tgLastState;
      var fn = window.__tgRenderKillSwitchBanner;
      if (lastMain && typeof fn === "function") fn(lastMain, name);
    } catch (e) { /* never break exec render */ }

    // v7.47.0 -- per-portfolio v10 strip + filtered activity feed.
    // v7.50.0 -- pass `data` so the renderer can read broker
    // trades_today.length (correct in both ORB_PORTFOLIO_FIRE modes).
    try { renderV10PerPortfolio(name, panel, data); }
    catch (e) { /* never break exec render */ }

    // Dim the whole panel when the executor is not configured so the
    // layout reads as "present but inert" rather than broken.
    panel.style.opacity = disabled ? "0.55" : "";

    // Banner: show the disabled / unhealthy state up top, hide otherwise.
    const banner = execField(panel, "banner");
    if (banner) {
      if (disabled) {
        banner.classList.remove("hide");
        banner.textContent = `${label} executor not configured \u2014 set ${name.toUpperCase()}_ALPACA_PAPER_KEY/SECRET (see .env.example).`;
      } else if (data.error) {
        banner.classList.remove("hide");
        banner.textContent = data.error;
      } else {
        banner.classList.add("hide");
        banner.textContent = "";
      }
    }

    // KPI row ----------------------------------------------------------
    // v4.0.4 — mirror Main's KPI row exactly. Equity / Cash / BP come
    // from the executor snapshot; Day P&L is computed server-side as
    // (equity \u2212 last_equity) from Alpaca's account object. Gate /
    // Regime / Session are market-wide and read from Main's /api/state
    // (republished on window.__tgLastState) so they match across tabs.
    const account = (data && data.account) || {};
    const positions = Array.isArray(data && data.positions) ? data.positions : [];
    const equity = disabled ? null : (account.equity ?? null);
    const lastEquity = disabled ? null : (account.last_equity ?? null);
    const dayPnl = disabled ? null : (account.day_pnl ?? null);
    const cash = disabled ? null : (account.cash ?? null);
    const bp = disabled ? null : (account.buying_power ?? null);
    let invested = 0.0, shorted = 0.0;
    for (const p of positions) {
      const gross = (Number(p.qty) || 0) * (Number(p.avg_entry) || 0);
      if (p.side === "SHORT") shorted += gross; else invested += gross;
    }

    setField(panel, "k-equity", fmtUsd(equity));
    setFieldHtml(panel, "k-equity-sub",
      disabled
        ? "\u2014"
        : `Cash ${esc(fmtUsd(cash))} \u00b7 BP ${esc(fmtUsd(bp))}`
    );

    // Day P&L: prefer Alpaca's (equity \u2212 last_equity) so the value
    // matches the same portfolio math Main uses. Falls back to em-dash
    // when either leg is missing (never a literal "+").
    const pnlEl = execField(panel, "k-pnl");
    if (pnlEl) {
      pnlEl.textContent = fmtUsd(dayPnl);
      pnlEl.classList.remove("delta-up", "delta-down");
      if (!disabled && dayPnl !== null && !isNaN(dayPnl)) {
        pnlEl.classList.add(dayPnl >= 0 ? "delta-up" : "delta-down");
      }
    }
    if (disabled || dayPnl === null || lastEquity === null || !lastEquity) {
      setField(panel, "k-pnl-sub", "\u2014");
    } else {
      const pct = (dayPnl / lastEquity) * 100;
      const sign = pct >= 0 ? "+" : "\u2212";
      setFieldHtml(panel, "k-pnl-sub",
        `vs close \u00b7 <span class="${pct >= 0 ? 'delta-up' : 'delta-down'}">${sign}${Math.abs(pct).toFixed(2)}%</span>`
        + ' <span style="color:var(--text-dim);font-size:10px" title="Day P&L source: Alpaca broker (equity minus prior-day close). Main tab shows paper state.">Alpaca</span>'
      );
    }

    setField(panel, "k-open", disabled ? "\u2014" : String(positions.length));
    if (disabled) {
      setField(panel, "k-open-sub", "\u2014");
    } else if (!positions.length) {
      setField(panel, "k-open-sub", "No positions");
    } else {
      const longs = positions.filter(p => p.side !== "SHORT").length;
      const shorts = positions.length - longs;
      setField(panel, "k-open-sub", `${longs} long \u00b7 ${shorts} short`);
    }

    // Session KPI — shared market state. Pull from Main's last
    // /api/state so every tab shows the same value. v5.17.0 — Gate +
    // Regime tiles dropped (those are market-wide and live only on Main).
    const ms = window.__tgLastState || {};
    const reg = ms.regime || {};
    const sEl = execField(panel, "k-session");
    if (sEl) {
      const mode = reg.mode || "\u2014";
      sEl.textContent = mode;
      // v5.31.4 — read from window. The helper lives in the main IIFE;
      // referencing it as a bare identifier here throws ReferenceError.
      const _color = (typeof window !== "undefined" && window.__tgSessionColor)
        ? window.__tgSessionColor(mode)
        : "var(--up)";
      sEl.style.color = _color;
    }
    setField(panel, "k-session-sub", reg.mode_reason || "\u2014");

    // Open positions card ----------------------------------------------
    const posBody = execField(panel, "pos-body");
    const posCount = execField(panel, "pos-count");
    if (posCount) posCount.textContent = "\u00b7 " + (disabled ? "0" : positions.length);  // "0" not "\u2014" when disabled; total = ORB + EOD (both Alpaca positions)

    if (posBody) {
      if (disabled) {
        posBody.innerHTML = `<div class="empty">\u2014</div>`;
      } else if (!positions.length) {
        posBody.innerHTML = `<div class="empty">No open positions.</div>`;
      } else {
        // v6.0.3: Stop column added for parity with the Main positions
        // table. The /api/executor/<name> payload doesn't carry stop
        // levels (those live on the engine state, not the broker), so we
        // cross-reference Main's last /api/state by symbol. window.__tgLastState
        // is published by Main on every poll. Falls back to em-dash when
        // Main hasn't populated yet (initial page load before first state
        // tick) or the symbol isn't tracked there (shouldn't happen, but
        // we don't crash).
        const _mainState = (typeof window !== "undefined" && window.__tgLastState) || {};
        const _mainPositions = Array.isArray(_mainState.positions) ? _mainState.positions : [];
        const _stopBySym = {};
        for (const _mp of _mainPositions) {
          if (!_mp || !_mp.ticker) continue;
          const _eff = (typeof _mp.effective_stop === "number")
                         ? _mp.effective_stop : _mp.stop;
          const _trailArmed = !!(_mp.trail_pill && _mp.trail_pill.status);
          var _entryStop = (typeof _mp.entry_stop === "number" && _mp.entry_stop > 0)
                             ? _mp.entry_stop : _mp.stop;
          _stopBySym[_mp.ticker] = {
            eff: _eff, trail: _trailArmed, entry_stop: _entryStop,
          };
        }
        // v9.1.50 \u2014 FIRE=1 independent mode: Val/Gene can hold tickers
        // not in Main's paper_state. Fill gaps from engine_positions
        // (stop/entry_stop keyed by ticker, added by dashboard_server).
        const _engPos = (data && data.engine_positions) || {};
        for (const [_sym, _ep] of Object.entries(_engPos)) {
          if (!_stopBySym[_sym] && Number.isFinite(_ep.stop) && _ep.stop > 0) {
            _stopBySym[_sym] = {
              eff: _ep.stop,
              trail: false,
              entry_stop: (_ep.entry_stop > 0 ? _ep.entry_stop : _ep.stop),
            };
          }
        }
        // EOD positions keyed by ticker; rendered below ORB so layout mirrors Main (ORB on top, EOD below).
        const _eodPos = (data && data.eod_positions) || {};
        const _orbPositions = positions.filter(p => !_eodPos[p.symbol]);
        const _eodPositions = positions.filter(p => !!_eodPos[p.symbol]);
        // v7.0.3 \u2014 match Main's positions <table> shape exactly.
        const rows = _orbPositions.map(p => {
          const sideCls = p.side === "SHORT" ? "side-short" : "side-long";
          const markCls = p.side === "SHORT" ? "mark-short" : "mark-long";
          const pnlCls = (Number(p.unrealized_pnl) || 0) >= 0 ? "delta-up" : "delta-down";
          const dotTitle = (p.side === "SHORT") ? "Open short position" : "Open long position";
          const _stopInfo = _stopBySym[p.symbol] || null;
          let _stopTxt = "\u2014";
          if (_stopInfo && Number.isFinite(_stopInfo.eff)) {
            _stopTxt = fmtNum(_stopInfo.eff, 2);
            if (_stopInfo.trail) {
              _stopTxt += ` <span class="trail-badge" title="Trail stop is armed \u2014 the effective stop now follows price, not the original hard stop">TRAIL</span>`;
            }
          }
          // v7.42.0 -- progress bar uses stop from Main state (cross-
          // referenced above), entry from broker (avg_entry), mark from
          // broker (current_price). Target derived via RR=2.5.
          // v9.1.5 -- axis anchored on the IMMUTABLE admission stop
          // (_stopInfo.entry_stop) so the 1R / target ticks don't drift
          // when the chandelier trail moves the live stop past entry.
          // _stopInfo.eff is overlaid as a separate "trail" tick.
          // Only ORB positions reach here; EOD positions rendered separately below.
          var _progressRow = "";
          var _axisStopForBar = _stopInfo
              && Number.isFinite(_stopInfo.entry_stop)
              && _stopInfo.entry_stop > 0
                ? _stopInfo.entry_stop
                : (_stopInfo && Number.isFinite(_stopInfo.eff) ? _stopInfo.eff : null);
          var _effStopForBar = _stopInfo && Number.isFinite(_stopInfo.eff)
                                 ? _stopInfo.eff : null;
          var _entryForBar = Number(p.avg_entry);
          var _markForBar  = Number(p.current_price);
          if (_axisStopForBar != null
              && Number.isFinite(_entryForBar) && _entryForBar > 0
              && Number.isFinite(_markForBar)  && _markForBar  > 0
              && Math.abs(_entryForBar - _axisStopForBar) > 1e-4) {
            var _isLong = p.side !== "SHORT";
            var _stopForBar = _axisStopForBar;
            var _targetForBar = _isLong
              ? _entryForBar + 2.5 * (_entryForBar - _stopForBar)
              : _entryForBar - 2.5 * (_stopForBar - _entryForBar);
            var _span = _targetForBar - _stopForBar;
            var _exPct = function(px) {
              if (Math.abs(_span) < 1e-9) return 50;
              return Math.max(0, Math.min(100, (px - _stopForBar) / _span * 100));
            };
            var _entryAt = _exPct(_entryForBar);
            var _oneRPx = _isLong
              ? _entryForBar + (_entryForBar - _stopForBar)
              : _entryForBar - (_stopForBar - _entryForBar);
            var _oneRAt = _exPct(_oneRPx);
            var _markAt = _exPct(_markForBar);
            var _r = _isLong
              ? (_markForBar - _entryForBar) / (_entryForBar - _stopForBar)
              : (_entryForBar - _markForBar) / (_stopForBar - _entryForBar);
            var _rTxt = (_r >= 0 ? "+" : "") + _r.toFixed(2) + "R";
            var _trailTick = "";
            if (_effStopForBar != null
                && Math.abs(_effStopForBar - _axisStopForBar) > 1e-4) {
              var _trailAt = _exPct(_effStopForBar);
              _trailTick = '<span class="pos-progress-tick trail" '
                + 'style="left:' + _trailAt.toFixed(2) + '%" '
                + 'data-label="trail" '
                + 'title="effective stop (trail): ' + fmtNum(_effStopForBar, 2) + '"></span>';
            }
            _progressRow =
              '<tr class="pos-progress-row" data-pos-ticker="' + esc(p.symbol) + '">' +
                '<td colspan="11" class="pos-progress-cell">' +
                  '<div class="pos-progress">' +
                    '<div class="pos-progress-track">' +
                      '<div class="pos-progress-zone red"     style="left:0%; width:' + _entryAt.toFixed(2) + '%"></div>' +
                      '<div class="pos-progress-zone neutral" style="left:' + _entryAt.toFixed(2) + '%; width:' + (_oneRAt - _entryAt).toFixed(2) + '%"></div>' +
                      '<div class="pos-progress-zone green"   style="left:' + _oneRAt.toFixed(2) + '%; width:' + (100 - _oneRAt).toFixed(2) + '%"></div>' +
                      '<span class="pos-progress-tick" style="left:' + _entryAt.toFixed(2) + '%" data-label="entry"></span>' +
                      '<span class="pos-progress-tick" style="left:' + _oneRAt.toFixed(2) + '%" data-label="1R"></span>' +
                      '<span class="pos-progress-tick end" style="left:100%" data-label="target"></span>' +
                      _trailTick +
                      '<span class="pos-progress-needle ' + (_r >= 0 ? 'up' : 'down') + '" style="left:' + _markAt.toFixed(2) + '%">' +
                        '<span class="needle-label">' + esc(_rTxt) + '</span>' +
                      '</span>' +
                    '</div>' +
                    '<div class="pos-progress-meta">' +
                      '<span class="pp-meta-left">stop ' + fmtNum((_effStopForBar != null ? _effStopForBar : _stopForBar), 2) + '</span>' +
                      '<span class="pp-meta-center">1R ' + fmtNum(_oneRPx, 2) + '</span>' +
                      '<span class="pp-meta-right">target ' + fmtNum(_targetForBar, 2) + '</span>' +
                    '</div>' +
                  '</div>' +
                '</td>' +
              '</tr>';
          }
          // v7.89.0 -- Notional column (mirrors the Main table column
          // added in v7.87.0). For longs it's the dollar amount
          // invested; for shorts it's the dollar liability outstanding.
          var _notionalTxt = (function(){
            var s=Number(p.qty), e=Number(p.avg_entry);
            if (!(s>0 && e>0)) return "\u2014";
            return fmtUsd(s*e);
          })();
          // v9.1.9 -- cross-tab parity with Main's renderPositions: each
          // open position is expandable on click, revealing the same
          // intraday chart the v10 Proximity matrix uses (shared via
          // window.__tgRenderTickerChart). Expansion state lives on the
          // posBody element so it survives re-renders.
          // Phase badge (OPEN / 1R↗ / TRAIL) from engine_positions flags.
          // EOD positions are now in a separate section below, so all rows
          // here are ORB positions (v9.1.66).
          var _phaseBadge = "";
          var _epData = _engPos[p.symbol] || null;
          if (_epData) {
            var _phA = !_epData.partial_taken;
            var _phC = _epData.partial_taken && _epData.be_moved;
            var _phB = _epData.partial_taken && !_epData.be_moved;
            var _phLabel = _phA ? "OPEN" : _phB ? "1R↗" : "TRAIL";
            var _phCls = _phA ? "A" : _phB ? "B" : "C";
            var _phTitle = _phA ? "Initial entry (stop at hard stop)" : _phB ? "1R partial taken, arming toward BE" : "Mature runner, stop at breakeven";
            _phaseBadge = '<span class="eot-phase-badge eot-phase-' + _phCls + '" title="' + _phTitle + '">' + _phLabel + '</span>';
          }
          var _expanded = posBody.__posExpanded && posBody.__posExpanded.has(p.symbol);
          var _chartRow = _expanded
            ? '<tr class="pos-chart-row" data-pos-chart="' + esc(p.symbol) + '">'
              + '<td colspan="11" class="pos-chart-cell">'
              + '<div class="pos-chart-mount" data-chart-mount="' + esc(p.symbol) + '"></div>'
              + '</td></tr>'
            : '';
          return `<tr data-pos-ticker="${esc(p.symbol)}" tabindex="0" role="button" aria-expanded="${_expanded ? 'true' : 'false'}" style="cursor:pointer">
            <td><span class="ticker">${esc(p.symbol)} <span class="mark ${markCls}" title="${esc(dotTitle)}">\u25cf</span></span>${_phaseBadge}</td>
            <td><span class="${sideCls}">${esc(p.side)}</span></td>
            <td class="right">${fmtNum(p.qty, 0)}</td>
            <td class="right">${fmtNum(p.avg_entry, 2)}</td>
            <td class="right">${fmtNum(p.current_price, 2)}</td>
            <td class="right" title="Notional at cost: shares \u00d7 entry. Long = invested $; short = liability $. Feeds the 95%-of-equity total-exposure cap (v7.86.0).">${_notionalTxt}</td>
            <td class="right">${_stopTxt}</td>
            <td class="right" title="Risk dollars at the effective stop. |entry \u2212 stop| \u00d7 shares. Sums into the Concurrent Risk gauge.">${(function(){var s=Number(p.qty),e=Number(p.avg_entry),st=_stopInfo&&Number.isFinite(_stopInfo.eff)?_stopInfo.eff:NaN;if(!(s>0&&e>0&&Number.isFinite(st)))return "\u2014";var rps=Math.abs(e-st);return rps>0?fmtUsd(rps*s):"\u2014";})()}</td>
            <td class="right ${pnlCls}">${fmtUsd(p.unrealized_pnl)}</td>
            <td class="right ${pnlCls}">${fmtPctExec(p.unrealized_pnl_pct, 2)}</td>
            <td class="right" title="Time in position since entry (v8.3.18). Computed client-side from entry_ts_utc.">${(typeof window.fmtHeld==='function'?window.fmtHeld(p.entry_ts_utc):'—')}</td>
          </tr>${_progressRow}${_chartRow}`;
        }).join("");
        if (_orbPositions.length > 0) {
          posBody.innerHTML = `<table>
            <thead><tr>
              <th title="Symbol \u00b7 colored dot shows side (green = long, red = short)">Ticker</th>
              <th title="LONG = bought to open. SHORT = sold to open.">Side</th>
              <th class="right" title="Number of shares">Sh</th>
              <th class="right" title="Average fill price when the position opened">Entry</th>
              <th class="right" title="Latest mark price">Mark</th>
              <th class="right" title="Notional at cost: shares \u00d7 entry. Long = invested $; short = liability $. Feeds the 95%-of-equity total-exposure cap (v7.86.0).">Notional</th>
              <th class="right" title="Effective stop from the engine (Main state). TRAIL badge means the trail stop is armed.">Stop</th>
              <th class="right" title="Risk dollars at the effective stop. |entry \u2212 stop| \u00d7 shares. Sums into the Concurrent Risk gauge.">Risk</th>
              <th class="right" title="Unrealized profit/loss in dollars at the current mark">Unreal.</th>
              <th class="right" title="Unrealized P&L as a percent of cost basis (entry x shares)">%</th>
              <th class="right" title="Time in position since entry (v8.3.18). Computed client-side from entry_ts_utc.">Held</th>
            </tr></thead>
            <tbody>${rows}</tbody></table>`;
        } else {
          posBody.innerHTML = "";
        }

        // EOD positions below ORB; Val/Gene use live Alpaca marks (unlike Main which uses EodReversalEngine state).
        if (_eodPositions.length > 0) {
          var _eodEtMin = (typeof window.__tgNowEtMinutes === "function") ? window.__tgNowEtMinutes() : 0;
          var _eodWS = 15 * 60, _eodWE = 15 * 60 + 59;
          var _eodHtml = _eodPositions.map(function(p) {
            var _sc = p.side === "SHORT" ? "side-short" : "side-long";
            var _mc = p.side === "SHORT" ? "mark-short" : "mark-long";
            var _pnlC = (Number(p.unrealized_pnl) || 0) >= 0 ? "delta-up" : "delta-down";
            var _nt = (function(){ var s=Number(p.qty),e=Number(p.avg_entry); if(!(s>0&&e>0))return "—"; return fmtUsd(s*e); })();
            var _dotTitle = p.side === "SHORT" ? "Open short position" : "Open long position";
            var _eEl = Math.max(0, Math.min(59, _eodEtMin - _eodWS));
            var _ePct = (_eEl / 59) * 100;
            var _eRem = 59 - _eEl;
            var _eNC = p.side === "SHORT" ? "eod-needle-short" : "eod-needle-long";
            var _bar =
              '<tr class="pos-progress-row eod-time-bar" data-pos-ticker="' + esc(p.symbol) + '">' +
                '<td colspan="11" class="pos-progress-cell">' +
                  '<div class="pos-progress eod-progress">' +
                    '<div class="pos-progress-track">' +
                      '<div class="pos-progress-zone eod-elapsed" style="left:0%;width:' + _ePct.toFixed(1) + '%;border-radius:5px 0 0 5px"></div>' +
                      '<div class="pos-progress-zone eod-remain" style="left:' + _ePct.toFixed(1) + '%;width:' + (100 - _ePct).toFixed(1) + '%;border-radius:0 5px 5px 0"></div>' +
                      '<span class="pos-progress-needle ' + _eNC + '" style="left:' + _ePct.toFixed(1) + '%">' +
                        '<span class="needle-label">' + _eEl + 'm</span>' +
                      '</span>' +
                    '</div>' +
                    '<div class="pos-progress-meta">' +
                      '<span class="pp-meta-left">15:00 entry</span>' +
                      '<span class="pp-meta-center">' + _eRem + 'm to EOD exit</span>' +
                      '<span class="pp-meta-right">15:59 exit</span>' +
                    '</div>' +
                  '</div>' +
                '</td>' +
              '</tr>';
            return '<tr data-pos-ticker="' + esc(p.symbol) + '">' +
              '<td><span class="ticker">' + esc(p.symbol) + ' <span class="mark ' + _mc + '" title="' + esc(_dotTitle) + '">●</span></span><span class="eod-badge">EOD</span></td>' +
              '<td><span class="' + _sc + '">' + esc(p.side) + '</span></td>' +
              '<td class="right">' + fmtNum(p.qty, 0) + '</td>' +
              '<td class="right">' + fmtNum(p.avg_entry, 2) + '</td>' +
              '<td class="right">' + fmtNum(p.current_price, 2) + '</td>' +
              '<td class="right">' + _nt + '</td>' +
              '<td class="right">—</td>' +
              '<td class="right">—</td>' +
              '<td class="right ' + _pnlC + '">' + fmtUsd(p.unrealized_pnl) + '</td>' +
              '<td class="right ' + _pnlC + '">' + fmtPctExec(p.unrealized_pnl_pct, 2) + '</td>' +
              '<td class="right">' + (typeof window.fmtHeld === "function" ? window.fmtHeld(p.entry_ts_utc) : "—") + '</td>' +
            '</tr>' + _bar;
          }).join("");
          posBody.innerHTML += (_orbPositions.length > 0 ? '<div class="eod-section-sep"></div>' : '') +
            '<table class="eod-pos-table"><tbody>' + _eodHtml + '</tbody></table>';
        }

        // v9.1.9 -- click-to-expand parity with Main's renderPositions.
        // Toggle the ticker in posBody.__posExpanded and re-render via
        // window.__tgRenderExecutor(name, cachedData) so the chart row
        // is inserted/removed deterministically. The handler is wired
        // once via sentinel; on each click it resolves the exec name
        // from the panel id ("tg-panel-val" / "tg-panel-gene") and
        // pulls the latest data from window.__tgLastExecData, so the
        // closure doesn't go stale across renders.
        if (!posBody.__posExpanded) posBody.__posExpanded = new Set();
        if (!posBody.__posClickWired) {
          posBody.addEventListener("click", function _exPosRowClick(ev) {
            if (ev.target.closest("tr.pos-progress-row")) return;
            if (ev.target.closest("tr.pos-chart-row")) return;
            var tr = ev.target.closest("tr[data-pos-ticker]");
            if (!tr) return;
            var tk = tr.getAttribute("data-pos-ticker");
            if (!tk) return;
            if (posBody.__posExpanded.has(tk)) {
              posBody.__posExpanded.delete(tk);
            } else {
              posBody.__posExpanded.add(tk);
            }
            // Re-render through the public executor entry point with
            // the live (not closure-captured) data.
            try {
              var pId = (panel && panel.id) || "";
              var execName = pId.replace(/^tg-panel-/, "");
              var liveData = (window.__tgLastExecData || {})[execName];
              if (typeof window.__tgRenderExecutor === "function" && liveData) {
                window.__tgRenderExecutor(execName, liveData);
              }
            } catch (_e) { /* fall through to next state poll */ }
          });
          posBody.addEventListener("keydown", function _exPosRowKey(ev) {
            if (ev.key !== "Enter" && ev.key !== " ") return;
            var tr = ev.target.closest("tr[data-pos-ticker]");
            if (!tr) return;
            ev.preventDefault();
            tr.click();
          });
          posBody.__posClickWired = true;
        }

        // Hydrate any chart mounts. Shared pipeline with Main.
        try {
          var _mountFn = (typeof window !== "undefined") && window.__tgRenderTickerChart;
          if (typeof _mountFn === "function") {
            posBody.querySelectorAll('.pos-chart-row [data-chart-mount]').forEach(function (mount) {
              var tk = mount.getAttribute("data-chart-mount");
              if (tk) _mountFn(tk, mount);
            });
          }
        } catch (_e) { /* never break the executor renderer */ }
      }
    }

    // v7.89.0 -- port-strip footer (Cash / BP / Invested / Shorted)
    // retired from the Open Positions card on Val/Gene tabs. Equity
    // sits in the KPI row above and per-position notional is shown
    // in the table's Notional column (v7.89.0).

    // v5.23.0 — Last signal card removed (was backed by in-memory
    // global that reset on redeploy, so almost always null).

    // Diagnostics ------------------------------------------------------
    setField(panel, "d-account", account.account_number || "\u2014");
    setField(panel, "d-status", account.status || "\u2014");
    setField(panel, "d-baseurl", (data && data.alpaca_base_url) || "\u2014");
    setField(panel, "d-error", (data && data.error) || "\u2014");

    // Market-state widgets (shared) + per-executor Today's Trades -----
    renderExecMarketState(panel);
    renderExecTrades(panel, data, disabled);
  }

  // v9.1.9 -- expose renderExecutor + the last per-executor data so the
  // inline position-row click handler can trigger a deterministic
  // re-render without waiting for the next state poll. The cache is
  // keyed by exec name ("val" / "gene") and refreshed on every render.
  if (typeof window !== "undefined") {
    window.__tgRenderExecutor = renderExecutor;
    window.__tgLastExecData = window.__tgLastExecData || {};
  }

  // Refresh the shared Session KPI cell on a given executor panel from
  // Main's last /api/state so the value matches across every tab.
  // v5.17.0 — Gate + Regime tiles dropped (those are market-wide
  // and live only on Main now); only Session remains shared.
  function refreshExecSharedKpis(panel) {
    const ms = window.__tgLastState || {};
    const reg = ms.regime || {};
    const sEl = execField(panel, "k-session");
    if (sEl) {
      const mode = reg.mode || "\u2014";
      sEl.textContent = mode;
      // v5.31.4 — read from window. Same scope-bridge as in renderExecutor.
      const _color = (typeof window !== "undefined" && window.__tgSessionColor)
        ? window.__tgSessionColor(mode)
        : "var(--up)";
      sEl.style.color = _color;
    }
    setField(panel, "k-session-sub", reg.mode_reason || "\u2014");
  }

  // Whenever Main's /api/state poll lands, refresh the shared
  // market-state widgets + KPI cells on Val/Gene panels if their
  // skeletons are up.
  window.__tgOnState = function () {
    for (const exec of ["val", "gene"]) {
      const panel = $$("tg-panel-" + exec);
      if (panel && panel.dataset.tgReady) {
        renderExecMarketState(panel);
        refreshExecSharedKpis(panel);
        // v7.47.0 -- refresh per-portfolio v10 strip + activity feed
        // when Main's /api/state lands (the v10 block lives there,
        // not on the /api/executor/<name> endpoint).
        // v7.50.0 -- pass cached exec payload so broker trades_today
        // stays correct across Main /api/state-driven refreshes too.
        try { renderV10PerPortfolio(exec, panel, _execLastData[exec]); }
        catch (e) { /* never break shared refresh */ }
      }
    }
  };

  // Kick off the index strip (runs on all tabs) and poll every 30s.
  pollIndices();
  setInterval(pollIndices, 30000);

  // Pre-poll Val and Gene so their badges populate even before user
  // clicks the tab. Then poll the active tab every 15s.
  pollExecutor("val");
  pollExecutor("gene");
  setInterval(() => {
    if (activeTab === "val" || activeTab === "gene") pollExecutor(activeTab);
  }, 15000);

  // v5.13.6 — Lifecycle tab
  // -------------------------------------------------------------------
  (function () {
    const PANEL = document.getElementById("tg-panel-lifecycle");
    if (!PANEL) return;
    const filterEl = document.getElementById("lifecycle-filter");
    const posEl = document.getElementById("lifecycle-position");
    const refreshBtn = document.getElementById("lifecycle-refresh");
    const timelineEl = document.getElementById("lifecycle-timeline");
    const countEl = document.getElementById("lifecycle-count");
    const evCountEl = document.getElementById("lifecycle-event-count");
    const summaryEl = document.getElementById("lifecycle-position-summary");
    const statusEl = document.getElementById("lifecycle-status");
    const badgeEl = document.getElementById("tg-badge-lifecycle");

    const TYPE_COLORS = {
      ENTRY_DECISION:   "#22c55e",
      PHASE1_EVAL:      "#60a5fa",
      PHASE2_EVAL:      "#60a5fa",
      PHASE3_CANDIDATE: "#60a5fa",
      PHASE4_SENTINEL:  "#3b82f6",
      TITAN_GRIP_STAGE: "#a78bfa",
      ORDER_SUBMIT:     "#facc15",
      ORDER_FILL:       "#facc15",
      ORDER_CANCEL:     "#f97316",
      EXIT_DECISION:    "#ef4444",
      POSITION_CLOSED:  "#ef4444",
      REASON:           "#94a3b8",
    };

    let lastSeq = 0;
    let pollTimer = null;
    let activePositionId = "";

    async function fetchPositions() {
      const status = filterEl ? filterEl.value : "all";
      try {
        const r = await fetch("/api/lifecycle/positions?status=" + encodeURIComponent(status) + "&limit=40", { credentials: "same-origin" });
        if (!r.ok) throw new Error("http " + r.status);
        const data = await r.json();
        const rows = (data && data.positions) || [];
        if (countEl) countEl.textContent = "· " + rows.length;
        if (badgeEl) badgeEl.textContent = rows.length ? String(rows.length) : "—";
        if (statusEl) statusEl.textContent = rows.length ? (status + " " + rows.length) : "no positions";
        if (posEl) {
          const cur = posEl.value;
          posEl.innerHTML = '<option value="">— select a position —</option>' + rows.map(r => {
            // v7.82.0 -- display in browser-local timezone (was raw UTC).
            const label = (r.ticker || "?") + " " + (r.side || "") + " " +
              window.utcIsoToLocalFull(r.entry_ts_utc || "") + " (" + (r.status || "") + ")";
            // v5.13.10 — surface position_id and any cached realized P&L / latest stage in the option tooltip.
            const tipParts = ["position_id: " + (r.position_id || "")];
            if (r.realized_pnl !== undefined && r.realized_pnl !== null) tipParts.push("realized: $" + Number(r.realized_pnl).toFixed(2));
            if (r.latest_titan_stage !== undefined && r.latest_titan_stage !== null) tipParts.push("titan stage: " + r.latest_titan_stage);
            if (r.latest_phase4_state) tipParts.push("phase4: " + r.latest_phase4_state);
            const tip = tipParts.join(" \u00b7 ");
            return '<option value="' + escAttr(r.position_id) + '" title="' + escAttr(tip) + '">' + escHtml(label) + '</option>';
          }).join("");
          if (cur && rows.some(r => r.position_id === cur)) posEl.value = cur;
        }
      } catch (e) {
        if (statusEl) statusEl.textContent = "error: " + e.message;
      }
    }

    function escHtml(s) { return String(s == null ? "" : s).replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c])); }
    function escAttr(s) { return escHtml(s).replace(/"/g, "&quot;"); }

    // v5.13.10 — per-event-type human-readable description for the chip tooltip.
    const TYPE_TOOLTIPS = {
      ENTRY_DECISION:   "Entry decision — a position was opened. Payload includes entry/limit/stop prices, share count, OR_high, PDC, and a snapshot of Phase 1/2/3 gate state at fire-time.",
      PHASE1_EVAL:      "Phase 1 evaluation — index regime + permit (long/short) recomputed.",
      PHASE2_EVAL:      "Phase 2 evaluation — per-ticker volume gate and 5m boundary-hold check.",
      PHASE3_CANDIDATE: "Phase 3 candidate — a ticker armed for entry but did not yet fire (DI/NHOD/cross checks).",
      PHASE4_SENTINEL:  "Phase 4 sentinel — Sentinel Loop alarm status changed (or fired) on an open position. A1 Loss / A2 Flash / B Trend Death / C Vel. Ratchet / D HVP Lock / E Div. Trap.",
      TITAN_GRIP_STAGE: "Trail ratchet stage transition — stop anchor advanced to a new ratchet level.",
      ORDER_SUBMIT:     "Order submitted — broker order ticket sent (entry or close).",
      ORDER_FILL:       "Order filled — broker reported fill price and quantity.",
      ORDER_CANCEL:     "Order cancelled — the order ticket was cancelled before/after partial fill.",
      EXIT_DECISION:    "Exit decision — the engine decided to close (alarm trip, target, EOD, manual, etc.).",
      POSITION_CLOSED:  "Position closed — final realized P&L and hold duration recorded.",
      REASON:           "Reason note — free-form context appended by the engine."
    };

    // v5.13.10 — per-field tooltip strings used by the inline facts strip.
    const FIELD_TOOLTIPS = {
      entry_price:        "Decision-time price the engine used to open the position",
      limit_price:        "Limit price submitted on the order ticket",
      stop_price:         "Initial hard stop price set when the position opened",
      stop_capped:        "True if the stop was clamped by the spec\u2019s max-loss cap",
      shares:             "Number of shares on this fill / decision",
      qty:                "Order quantity (shares)",
      entry_num:          "1 = primary fill, 2 = second add",
      strike_num:         "Strike count (consecutive 5m closes above OR_high used to fire)",
      entry_id:           "Forensic id pairing this entry to its eventual close",
      or_high:            "5-minute opening-range high captured for this ticker",
      or_low:             "5-minute opening-range low captured for this ticker",
      pdc:                "Prior-day close (the regime anchor)",
      side:               "LONG = bought to open; SHORT = sold to open",
      fill_price:         "Actual fill price reported by the broker (paper or live)",
      notional:           "Shares × fill price (gross dollars)",
      order_type:         "limit / stop_market / market — ticket the close path would submit",
      action:             "open / close — which side of the lifecycle this order is on",
      exit_reason:        "Engine\u2019s normalized close reason (e.g. titan_a1, titan_b, eod, manual)",
      raw_reason:         "Free-form reason string before normalization",
      exit_price:         "Decision-time price the engine used to close the position",
      realized_pnl:       "Realized profit/loss in dollars after commissions",
      realized_pnl_pct:   "Realized profit/loss as a percent of entry notional",
      hold_seconds:       "Time the position was open, in seconds",
      alarm_codes:        "Sentinel alarm codes that fired this tick (A_LOSS=$ stop, A_FLASH=1-min velocity, B=QQQ vs 9-EMA, C=velocity ratchet, D=HVP lock, E=divergence trap)",
      fired:              "True if the sentinel actually closed the position this tick",
      current_price:      "Last mark price observed at sentinel evaluation time",
      state:              "Comma-joined alarm codes (or OK)",
      stage:              "Ratchet stage (0 = pre-arm, 1+ = trail engaged + ratcheting)",
      anchor:             "Trail anchor price the stop is measured from",
      shares_remaining:   "Shares still open after any partial harvest"
    };

    function _lcFmtVal(k, v) {
      if (v === null || v === undefined) return "—";
      if (typeof v === "number") {
        // Money-ish vs share-ish heuristics.
        if (k === "realized_pnl" || k === "notional") return "$" + v.toFixed(2);
        if (k === "realized_pnl_pct") return (v * 100).toFixed(2) + "%";
        if (k === "hold_seconds") {
          const m = Math.floor(v / 60), s = Math.round(v % 60);
          return m + "m" + (s < 10 ? "0" : "") + s + "s";
        }
        if (k === "shares" || k === "qty" || k === "entry_num" || k === "strike_num" ||
            k === "stage" || k === "shares_remaining") return String(v);
        // Default: prices and floats with up to 4 decimals trimmed.
        return v.toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
      }
      if (typeof v === "boolean") return v ? "yes" : "no";
      if (Array.isArray(v)) return v.join(",") || "—";
      const s = String(v);
      return s.length > 60 ? s.slice(0, 57) + "…" : s;
    }

    function _lcKeyOrder(et) {
      // Return preferred field order for each known event type.
      switch (et) {
        case "ENTRY_DECISION":   return ["entry_price","limit_price","stop_price","shares","entry_num","strike_num","or_high","pdc","stop_capped","entry_id"];
        case "ORDER_SUBMIT":     return ["side","qty","limit_price","price","order_type","action","raw_reason"];
        case "ORDER_FILL":       return ["side","qty","fill_price","notional","order_type","action"];
        case "ORDER_CANCEL":     return ["side","qty","reason"];
        case "EXIT_DECISION":    return ["exit_reason","exit_price","entry_price","shares","raw_reason"];
        case "POSITION_CLOSED":  return ["realized_pnl","realized_pnl_pct","hold_seconds","exit_reason"];
        case "PHASE4_SENTINEL":  return ["state","alarm_codes","current_price","fired","exit_reason"];
        case "TITAN_GRIP_STAGE": return ["stage","anchor","shares_remaining"];
        default: return [];
      }
    }

    function _lcFactsStrip(ev) {
      const p = ev.payload || {};
      const order = _lcKeyOrder(ev.event_type);
      // Show known/ordered keys first, then any remaining flat scalar keys.
      const seen = new Set();
      const parts = [];
      const push = (k) => {
        if (seen.has(k)) return;
        if (!(k in p)) return;
        const v = p[k];
        // Skip nested objects / large arrays — the JSON pre handles those.
        if (v !== null && typeof v === "object" && !Array.isArray(v)) return;
        if (Array.isArray(v) && v.length > 8) return;
        seen.add(k);
        const tip = FIELD_TOOLTIPS[k] || k;
        parts.push(
          '<span class="lifecycle-fact" title="' + escAttr(tip) + '" ' +
          'style="display:inline-flex;gap:4px;align-items:baseline;padding:1px 6px;margin:0 4px 2px 0;' +
          'background:var(--surface-2);border:1px solid var(--border);border-radius:3px;' +
          'font-size:10.5px;font-family:monospace;color:var(--text-muted)">' +
          '<span style="color:#5b6572">' + escHtml(k) + '</span>' +
          '<span style="color:var(--text)">' + escHtml(_lcFmtVal(k, v)) + '</span>' +
          '</span>'
        );
      };
      order.forEach(push);
      // Add remaining scalar keys not already covered.
      Object.keys(p).forEach(k => {
        if (seen.has(k)) return;
        const v = p[k];
        if (v === null || typeof v === "object") return;
        push(k);
      });
      if (parts.length === 0) return "";
      return '<div class="lifecycle-facts" style="margin-top:4px;display:flex;flex-wrap:wrap" ' +
             'title="Click the row to expand the full JSON payload">' + parts.join("") + '</div>';
    }

    function renderEvents(events, append) {
      if (!timelineEl) return;
      if (!append) timelineEl.innerHTML = "";
      if (!events || events.length === 0) {
        if (!append) timelineEl.innerHTML = '<div class="empty">No events for this position.</div>';
        return;
      }
      const frag = document.createDocumentFragment();
      events.forEach(ev => {
        const color = TYPE_COLORS[ev.event_type] || "#64748b";
        const typeTip = TYPE_TOOLTIPS[ev.event_type] || ("Event type: " + ev.event_type);
        const row = document.createElement("div");
        row.className = "lifecycle-row";
        row.style.cssText = "padding:8px 14px;border-bottom:1px solid var(--border);font-family:inherit;cursor:pointer";
        row.title = "Click to expand the full JSON payload";
        const reason = ev.reason_text ? '<div style="color:var(--text-dim);font-size:11px;margin-top:2px" title="Engine\u2019s short note describing why this event fired">' + escHtml(ev.reason_text) + '</div>' : "";
        const facts = _lcFactsStrip(ev);
        row.innerHTML =
          '<div style="display:flex;gap:10px;align-items:baseline;flex-wrap:wrap">' +
          '  <span style="font-size:10px;color:#5b6572;font-family:monospace" title="Per-position event sequence number (monotonically increasing)">#' + (ev.event_seq || 0) + '</span>' +
          '  <span style="font-size:10.5px;color:var(--text-dim);font-family:monospace" title="Event timestamp in your local timezone (stored as UTC: ' + escAttr(ev.event_ts_utc || "") + ')">' + escHtml(window.utcIsoToLocalFull(ev.event_ts_utc || "")) + '</span>' +
          '  <span class="lifecycle-chip" title="' + escAttr(typeTip) + '" style="background:' + color + '22;color:' + color + ';border:1px solid ' + color + '55;padding:1px 7px;border-radius:9px;font-size:10.5px;letter-spacing:.04em">' + escHtml(ev.event_type) + '</span>' +
          '</div>' + reason + facts +
          '<pre class="lifecycle-payload" title="Full raw event payload (JSON)" style="display:none;margin:6px 0 0;padding:8px;background:var(--surface-2);border-radius:4px;font-size:11px;color:var(--text-muted);max-height:300px;overflow:auto">' + escHtml(JSON.stringify(ev.payload || {}, null, 2)) + '</pre>';
        row.addEventListener("click", () => {
          const pre = row.querySelector(".lifecycle-payload");
          if (pre) pre.style.display = (pre.style.display === "none") ? "block" : "none";
        });
        frag.appendChild(row);
        if (Number(ev.event_seq) > lastSeq) lastSeq = Number(ev.event_seq);
      });
      timelineEl.appendChild(frag);
      if (evCountEl) evCountEl.textContent = "· " + timelineEl.querySelectorAll(".lifecycle-row").length;
    }

    async function fetchTimeline(positionId, since) {
      try {
        const url = "/api/lifecycle/" + encodeURIComponent(positionId) + (since ? ("?since_seq=" + since) : "");
        const r = await fetch(url, { credentials: "same-origin" });
        if (!r.ok) throw new Error("http " + r.status);
        const data = await r.json();
        const events = (data && data.events) || [];
        renderEvents(events, !!since);
        if (summaryEl) {
          const evList = timelineEl.querySelectorAll(".lifecycle-row");
          // v5.13.10 — also show the latest event type for quick orientation.
          const lastChip = timelineEl.querySelector(".lifecycle-row:last-child .lifecycle-chip");
          const lastTxt = lastChip ? lastChip.textContent.trim() : "";
          summaryEl.textContent = positionId + " · " + evList.length + " events" + (lastTxt ? (" · latest: " + lastTxt) : "");
          summaryEl.title = "Position id, total event count for this timeline, and the most recent event type";
        }
      } catch (e) {
        if (summaryEl) summaryEl.textContent = "error: " + e.message;
      }
    }

    function selectPosition(positionId) {
      activePositionId = positionId || "";
      lastSeq = 0;
      if (timelineEl) timelineEl.innerHTML = '<div class="empty">Loading…</div>';
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
      if (!activePositionId) {
        if (timelineEl) timelineEl.innerHTML = '<div class="empty">Select a position to view its timeline.</div>';
        return;
      }
      fetchTimeline(activePositionId, 0);
      // Tail-follow: poll for new events every 2s. Closed positions
      // won't grow but the cost of one HTTP/304-equivalent every 2s is
      // low and the file is local-disk-cheap server-side.
      pollTimer = setInterval(() => {
        if (!activePositionId) return;
        fetchTimeline(activePositionId, lastSeq);
      }, 2000);
    }

    if (filterEl) filterEl.addEventListener("change", () => fetchPositions());
    if (posEl) posEl.addEventListener("change", () => selectPosition(posEl.value));
    if (refreshBtn) refreshBtn.addEventListener("click", () => {
      fetchPositions();
      if (activePositionId) fetchTimeline(activePositionId, 0);
    });

    window.__tgLifecycleActivate = function () {
      fetchPositions();
    };
  })();

  /* ========================================================================
     v7.20.0 — v10 ORB Day Status banner + Projection card renderers.
     Consumes /api/state.v10 (delivered as part of every state poll) and
     /api/v10/projection (separate 60s poll for the static keystone numbers
     plus live account growth).
     ======================================================================== */

  // ============================================================
  // v7.40.0 -- kill-switch banner
  //
  // Reads several state surfaces to decide if any kill condition is
  // active, then renders a single banner summarizing all of them.
  // Each tab panel (main / val / gene) gets its own banner element
  // so the operator sees the kill state on the panel they're
  // currently viewing.
  //
  // Sources of kill state:
  //   - s.gates.scan_paused           (operator-paused scan loop)
  //   - s.gates.trading_halted        (legacy daily-loss halt)
  //   - s.v10.day_status.block_day    (VIX kill, missing VIX)
  //   - s.v10.risk_books[pid].daily_kill_triggered (v10 daily-kill)
  //   - s.v10.live_mode === false     (ORB_LIVE_MODE=0 kill switch)
  // ============================================================
  // v7.40.0 -- expose for the exec render path (Val/Gene poll loop)
  // which lives in a separate IIFE further below.
  // v7.41.0 -- also expose renderV10DayStatus for the same reason.
  if (typeof window !== "undefined") {
    Object.defineProperty(window, "__tgRenderKillSwitchBanner", {
      get: function () { return renderKillSwitchBanner; },
      configurable: true,
    });
    Object.defineProperty(window, "__tgRenderV10DayStatus", {
      get: function () { return renderV10DayStatus; },
      configurable: true,
    });
    // v7.44.0 -- expose ticker matrix renderer for smoke + future use
    Object.defineProperty(window, "__tgRenderV10TickerMatrix", {
      get: function () { return renderV10TickerMatrix; },
      configurable: true,
    });
    // v7.45.0 -- expose activity feed renderer
    Object.defineProperty(window, "__tgRenderV10ActivityFeed", {
      get: function () { return renderV10ActivityFeed; },
      configurable: true,
    });
    // v7.52.0 -- expose proximity-matrix renderer for the same
    // IIFE-1 / IIFE-2 routing pattern as the other v10 renderers.
    Object.defineProperty(window, "__tgRenderV10ProximityMatrix", {
      get: function () { return renderV10ProximityMatrix; },
      configurable: true,
    });
  }

  function renderKillSwitchBanner(s, target) {
    // Local HTML-escape to avoid cross-IIFE dependency on `escapeHtml`.
    function esc(v) {
      return String(v == null ? "" : v)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;")
        .replace(/>/g, "&gt;").replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
    }
    var hostId = target === "main" ? "ks-banner-main" : null;
    var banner = null;
    if (hostId) {
      banner = document.getElementById(hostId);
    } else {
      // val / gene panel: look up via [data-f="ks-banner"]
      var panel = document.getElementById("tg-panel-" + target);
      if (panel) banner = panel.querySelector('[data-f="ks-banner"]');
    }
    if (!banner) return;

    var v10 = (s && s.v10) || {};
    var gates = (s && s.gates) || {};
    var conditions = [];

    // 1. Operator paused the scan loop (manual /pause), OR auto-idle
    //    because we're outside RTH. v7.50.0 -- the backend now exposes
    //    these as two separate flags (scan_paused_user, scan_idle_hours)
    //    while keeping the legacy scan_paused union for compat. We pick
    //    the more specific message: outside-RTH wins because it's the
    //    most common reason the banner appears (every night + weekends).
    if (gates.scan_idle_hours) {
      conditions.push({
        title: "OUTSIDE MARKET HOURS",
        detail: "US RTH is closed. Scan loop auto-idle until the next open. No operator action required.",
        pid_chips: [],
      });
    } else if (gates.scan_paused_user || gates.scan_paused) {
      conditions.push({
        title: "SCAN PAUSED",
        detail: "Operator /pause active. New entries blocked; existing positions still managed to exit.",
        pid_chips: [],
      });
    }
    // 2. Legacy daily-loss halt (pre-v10)
    if (gates.trading_halted) {
      conditions.push({
        title: "TRADING HALTED",
        detail: (gates.halt_reason || "Legacy daily-loss halt active."),
        pid_chips: [],
      });
    }
    // 3. v10 kill switch (ORB_LIVE_MODE=0)
    if (v10 && v10.bootstrapped && v10.live_mode === false) {
      conditions.push({
        title: "V10 ORB DISABLED",
        detail: "ORB_LIVE_MODE=0. v10 strategy fully off; legacy path active.",
        pid_chips: [],
      });
    }
    // 4. Day-level block from day_gates
    var ds = v10.day_status || {};
    if (ds.block_day) {
      var reason = ds.block_reason || "unknown";
      // VIX kill / missing_vix / earnings / etc.
      conditions.push({
        title: "DAY BLOCKED",
        detail: "Day-level gate fired: " + reason +
          ". No new entries on any ticker today.",
        pid_chips: [],
      });
    }
    // 5. Per-portfolio daily-loss kill
    // v7.57.0 -- scope to `target` book only so the Main panel banner
    // doesn't surface val/gene kill state, and vice versa. Each tab is
    // independent.
    var rb = v10.risk_books || {};
    var killed_pids = [];
    var realized_total = 0;
    var threshold_total = 0;
    Object.keys(rb).sort().forEach(function (pid) {
      if (target && pid !== target) return;
      var book = rb[pid] || {};
      if (book.daily_kill_triggered) {
        killed_pids.push({
          pid: pid,
          realized: book.realized_pnl_today || 0,
          threshold: book.daily_kill_threshold || 0,
        });
        realized_total += (book.realized_pnl_today || 0);
        threshold_total += (book.daily_kill_threshold || 0);
      }
    });
    if (killed_pids.length > 0) {
      conditions.push({
        title: "DAILY-LOSS KILL ACTIVE",
        detail: "v10 daily-loss kill triggered. New entries blocked; " +
                "existing positions still managed to exit. ",
        pid_chips: killed_pids.map(function (k) {
          // When P&L has recovered to positive after the kill, show "+$X recovered"
          // instead of "$X / $-threshold" (which implies an ongoing loss).
          if (k.realized >= 0) {
            return k.pid.toUpperCase() + " +" +
                   "$" + Math.round(k.realized).toLocaleString() + " recovered";
          }
          return k.pid.toUpperCase() + " $" +
                 Math.round(k.realized).toLocaleString() +
                 " / $" + Math.round(-k.threshold).toLocaleString();
        }),
      });
    }

    if (conditions.length === 0) {
      banner.classList.add("hide");
      banner.classList.remove("killswitch-banner--info", "killswitch-banner--soft");
      banner.innerHTML = "";
      return;
    }
    banner.classList.remove("hide");

    var informationalOnly = (conditions.length === 1
                              && conditions[0].title === "OUTSIDE MARKET HOURS");
    // "Soft" state: only scan-paused + daily-kill active (expected mid-day — not critical).
    // The kill already fired, positions are still managed. This is NOT an emergency;
    // the operator doesn't need a bright red alarm for the rest of the afternoon.
    var SOFT_TITLES = {"SCAN PAUSED": true, "DAILY-LOSS KILL ACTIVE": true};
    var softPause = !informationalOnly && conditions.length >= 1
      && conditions.every(function(c) { return !!SOFT_TITLES[c.title]; });

    if (informationalOnly) {
      banner.classList.add("killswitch-banner--info");
      banner.classList.remove("killswitch-banner--soft");
    } else if (softPause) {
      banner.classList.add("killswitch-banner--soft");
      banner.classList.remove("killswitch-banner--info");
    } else {
      banner.classList.remove("killswitch-banner--info", "killswitch-banner--soft");
    }

    if (softPause) {
      // Condensed one-liner: no alarm icon, no heading, no button.
      var killCond = null;
      conditions.forEach(function(c) {
        if (c.title === "DAILY-LOSS KILL ACTIVE") killCond = c;
      });
      var chipHtml = '';
      if (killCond && killCond.pid_chips && killCond.pid_chips.length) {
        killCond.pid_chips.forEach(function(chip) {
          chipHtml += '<span class="ks-portfolio-chip">' + esc(chip) + '</span>';
        });
      }
      // When kill fired but P&L has since recovered to positive, "daily-loss limit
      // reached" is misleading — the loss that triggered the kill was temporary.
      var _killLabel = '';
      if (killCond) {
        var _pnlPositive = realized_total > 0;
        _killLabel = ' &mdash; '
          + (_pnlPositive ? 'morning session ended' : 'daily-loss limit reached')
          + (chipHtml ? ' ' + chipHtml : '');
      }
      banner.innerHTML = '<span class="ks-icon" aria-hidden="true">&#9646;</span>'
        + '<div class="ks-text"><div class="ks-detail">Scanner paused'
        + _killLabel
        + ' &middot; existing positions still managed</div></div>';
      return;
    }

    var icon = informationalOnly ? 'ℹ' : '⚠';
    var html = '<span class="ks-icon" aria-hidden="true">' + icon + '</span>'
             + '<div class="ks-text">';
    conditions.forEach(function (c, i) {
      html += (i === 0 ? '<div class="ks-title">' + esc(c.title) + '</div>' : '');
      if (i === 0) html += '<div class="ks-detail">';
      if (i > 0)  html += ' · <b>' + esc(c.title) + ':</b> ';
      html += esc(c.detail);
      if (c.pid_chips && c.pid_chips.length) {
        html += ' ';
        c.pid_chips.forEach(function(chip) {
          html += '<span class="ks-portfolio-chip">' + esc(chip) + '</span>';
        });
      }
    });
    html += '</div></div>';
    html += '<div class="ks-actions"><button type="button" class="ks-btn" '
         + 'onclick="window.scrollTo({top:document.body.scrollHeight,behavior:\'smooth\'})">'
         + 'View activity</button></div>';
    banner.innerHTML = html;
  }

  // v7.57.0 -- pidFilter scopes the banner to a single portfolio so the
  // Main tab only shows Main's gauges + per-ticker stats (operator
  // request: "Main, should only show information for main, it should
  // not show Val or Gene information"). Pass null to keep the legacy
  // cross-portfolio aggregation (no current caller; reserved for a
  // future cross-book overview view if we add one).
  // v9.1.0 -- EOD reversal addon renderer. Shared between Main banner
  // (DOM id v10-eod-section) and Val/Gene per-pid bodies (data-f
  // v10-eod-pid-body). When pidFilter is null/undefined, renders the
  // Main DOM. When pidFilter is set ("val" | "gene"), expects the
  // panel arg and renders into v10-eod-pid-body within that panel.
  function renderV10EodReversal(s, pidFilter, panel) {
    var v10 = s && s.v10;
    var eod = v10 && v10.eod;
    if (pidFilter && panel) {
      var hostBody = panel.querySelector('[data-f="v10-eod-pid-body"]');
      var hostStatus = panel.querySelector('[data-f="v10-eod-pid-status"]');
      var hostFire = panel.querySelector('[data-f="v10-eod-pid-fire"]');
      if (!hostBody) return;
      _v10EodFillBody(eod, pidFilter, hostBody, hostStatus, hostFire);
      return;
    }
    var section = document.getElementById("v10-eod-section");
    var body = document.getElementById("v10-eod-body");
    var statusEl = document.getElementById("v10-eod-status");
    var fireEl = document.getElementById("v10-eod-fire-pill");
    if (!section || !body) return;
    var orbSection = document.getElementById("v10-day-status");
    if (!eod || !eod.enabled) {
      section.style.display = "none";
      /* Restore full rounding on ORB card when EOD is hidden */
      if (orbSection) { orbSection.style.borderRadius = "10px"; orbSection.style.borderBottom = ""; }
      return;
    }
    // v9.1.32 -- always show the EOD card when enabled.
    section.style.display = "";
    /* Fuse ORB + EOD into one visual block: share borders, no gap between them */
    if (orbSection) {
      orbSection.style.borderRadius = "10px 10px 0 0";
      orbSection.style.borderBottom = "1px solid #374151";
    }
    section.style.borderRadius = "0 0 10px 10px";
    section.style.borderTop = "none";
    _v10EodFillBody(eod, "main", body, statusEl, fireEl, s);
  }

  function _v10EodFillBody(eod, pid, bodyEl, statusEl, fireEl, stateCtx) {
    if (!eod) { bodyEl.innerHTML = '<div style="color:var(--text-dim)">No EOD data.</div>'; return; }
    var perPid = (eod.per_portfolio || {})[pid] || {open_count:0, open_positions:[], closed_legs:[], realized_pnl_today:0, entry_attempted:false, rejected_count:0};
    var cfg = eod.config || {};
    var entryEt = cfg.entry_et || "15:00";
    var exitEt = cfg.exit_et || "15:59";
    var realized = parseFloat(perPid.realized_pnl_today || 0) || 0;
    var openCount = perPid.open_count || 0;
    var closedCount = (perPid.closed_legs || []).length;
    if (statusEl) {
      var parts = [];
      parts.push("window " + entryEt + "&#8209;" + exitEt + " ET");
      parts.push("open " + openCount);
      parts.push("closed " + closedCount);
      var realStr = (realized >= 0 ? "+" : "") + "$" + realized.toFixed(2);
      var realCls = realized >= 0 ? "delta-up" : "delta-down";
      parts.push('P&L <span class="' + realCls + '">' + realStr + '</span>');
      statusEl.innerHTML = parts.join(' <span style="color:var(--border-strong)">·</span> ');
    }
    if (fireEl) {
      var firing = !!cfg.fire_broker;
      // v9.1.32 -- LIVE mode gets a stronger visual treatment: larger
      // text, solid green background (not translucent), so real-order
      // mode is unmissable. Paper stays amber+translucent.
      if (firing) {
        fireEl.textContent = "LIVE ORDERS";
        fireEl.style.cssText = "padding:3px 10px;border-radius:999px;font-size:11px;font-weight:700;"
          + "background:#16a34a;color:#fff;letter-spacing:0.04em;";
      } else {
        fireEl.textContent = "paper";
        fireEl.style.cssText = "padding:2px 7px;border-radius:999px;font-size:11px;font-weight:600;"
          + "background:rgba(245,158,11,0.18);color:#f59e0b;";
      }
      fireEl.title = firing
        ? "ORB_EOD_FIRE_BROKER=1 — real broker orders will fire at " + entryEt + " ET."
        : "Paper-fire mode: signals tracked but no real orders. Set ORB_EOD_FIRE_BROKER=1 to go live.";
    }
    // v9.1.77 -- give the EOD card a green left-border accent when LIVE.
    // Previously always targeted #v10-eod-section (Main's element) even
    // when called for Val/Gene panels — the border was applied to the wrong
    // tab. Now scoped to the nearest card ancestor of bodyEl so each panel
    // gets its own accent independently.
    var _eodCard = bodyEl && bodyEl.closest
      ? bodyEl.closest(".card, section")
      : document.getElementById("v10-eod-section");
    if (!_eodCard) _eodCard = document.getElementById("v10-eod-section");
    if (_eodCard) {
      _eodCard.style.borderLeft = cfg.fire_broker ? "3px solid #22c55e" : "";
    }
    // Body: list open positions + closed legs.
    var rows = [];
    (perPid.open_positions || []).forEach(function (p) {
      var sideCol = p.side === "long" ? "var(--up)" : "var(--down)";
      // rev signal strength labeled clearly (was "rod3=X.Xbps" — internal name)
      var revBps = parseFloat(p.rod3_bps);
      var revHtml = Number.isFinite(revBps)
        ? '<span style="color:var(--text-muted);font-size:11px" title="Reversal signal strength (bps from VWAP)">rev ' + revBps.toFixed(1) + 'bps</span>'
        : '';
      rows.push(
        '<div style="display:flex;gap:10px;align-items:center">'
        + '<span style="color:' + sideCol + ';font-weight:700;min-width:46px">' + p.side.toUpperCase() + '</span>'
        + '<span style="color:var(--text);font-weight:700;min-width:46px">' + p.ticker + '</span>'
        + '<span style="color:var(--text-muted)">' + p.shares + ' sh @ $' + (parseFloat(p.entry_price)||0).toFixed(2) + '</span>'
        + revHtml
        + '<span style="color:var(--border-strong)">·</span>'
        + '<span style="color:var(--text-muted)">$' + Math.round(p.notional || 0).toLocaleString() + '</span>'
        + '</div>'
      );
    });
    (perPid.closed_legs || []).forEach(function (leg) {
      var pnl = parseFloat(leg.pnl) || 0;
      var pnlCls = pnl >= 0 ? "delta-up" : "delta-down";
      var sideCol = leg.side === "long" ? "var(--up)" : "var(--down)";
      rows.push(
        '<div style="display:flex;gap:10px;align-items:center;opacity:0.75">'
        + '<span style="color:' + sideCol + ';min-width:46px">' + leg.side.toUpperCase() + '</span>'
        + '<span style="color:var(--text);min-width:46px">' + leg.ticker + '</span>'
        + '<span style="color:var(--text-muted)">' + leg.shares + ' sh $' + (parseFloat(leg.entry_price)||0).toFixed(2) + ' → $' + (parseFloat(leg.exit_price)||0).toFixed(2) + '</span>'
        + '<span class="' + pnlCls + '" style="font-weight:700">' + (pnl >= 0 ? "+" : "") + '$' + pnl.toFixed(2) + '</span>'
        + '<span style="color:var(--text-dim)">' + (leg.exit_reason || 'eod') + '</span>'
        + '</div>'
      );
    });
    if (!rows.length) {
      // v9.1.32 -- show standby state when armed but not yet in the entry window.
      var _mode = ((stateCtx && stateCtx.regime || {}).mode || "");
      var _eodArmed = (_mode === "OPEN" || _mode === "OR" || _mode === "POWER" || !_mode);
      var msg = perPid.entry_attempted
        ? "No EOD signal admitted today (insufficient cross-section)."
        : _eodArmed
          ? "⏳ Armed — entry opens at " + entryEt + " ET."
          : "Session closed.";
      bodyEl.innerHTML = '<div style="color:var(--text-dim);font-size:12px">' + msg + '</div>';
    } else {
      bodyEl.innerHTML = rows.join('');
    }
  }

  function renderV10DayStatus(s, pidFilter) {
    var v10 = s && s.v10;
    var banner = document.getElementById("v10-day-status");
    if (!banner) return;
    // v9.1.0 -- render the EOD reversal card alongside the morning
    // v10 banner. Both feed off s.v10.* fields.
    try { renderV10EodReversal(s, null, null); } catch (_e) {}
    // Fail open: if v10 block is missing, hide the banner.
    if (!v10 || v10.available === false) {
      banner.style.display = "none";
      document.body.classList.remove("v10-live");
      return;
    }
    banner.style.display = "flex";
    // v7.27.0: flag the body so .legacy-v10-hidden sections collapse.
    // Set only when v10 is BOTH bootstrapped AND live-mode on; either
    // false leaves the legacy Permit Matrix visible as a safety net.
    if (v10.bootstrapped && v10.live_mode) {
      document.body.classList.add("v10-live");
    } else {
      document.body.classList.remove("v10-live");
    }

    var modePill = document.getElementById("v10-mode-pill");
    if (modePill) {
      var liveOn = !!v10.live_mode;
      var bootOk = !!v10.bootstrapped;
      if (!bootOk) {
        modePill.textContent = "BOOT";
        modePill.style.background = "#374151";
        modePill.style.color = "#e5e7eb";
      } else if (liveOn) {
        modePill.textContent = "LIVE";
        modePill.style.background = "#16a34a";
        modePill.style.color = "#fff";
      } else {
        modePill.textContent = "LEGACY";
        modePill.style.background = "#dc2626";
        modePill.style.color = "#fff";
      }
    }

    // v9.1.32 -- off-hours collapsed state. When session_date is empty
    // (bot hasn't run a session today yet — typically between 00:00 and
    // 09:25 ET) the gate pills show nothing meaningful (— / —). Replace
    // the entire gate section with a single CLOSED chip so the banner
    // doesn't look half-loaded all night. During an active session
    // (session_date non-empty) all gates render normally.
    var ds = v10.day_status || {};
    var _sessionActive = !!(ds.session_date);
    var _regimeMode = ((s.regime || {}).mode || "CLOSED");
    var _isMarketHours = (_regimeMode === "OPEN" || _regimeMode === "OR"
      || _regimeMode === "POWER" || _regimeMode === "PRE");
    var _showGatePills = _sessionActive || _isMarketHours;
    // Hide the full gate section (VIX | Day rows) when off-hours and
    // session not yet started. IDs are the v10-day-status pill spans.
    ["v10-vix-divider", "v10-vix-label", "v10-vix", "v10-vix-pass",
     "v10-day-divider", "v10-day-label", "v10-day-state",
     "v10-atr-pill", "v10-atr-pill-divider",
     "v10-partial-pill", "v10-partial-pill-divider",
     "v10-wash-pill", "v10-wash-pill-divider",
     "v10-spy-pill", "v10-spy-pill-divider",
     "v10-mbr-pill", "v10-mbr-pill-divider",
     "v10-chase-pill", "v10-chase-pill-divider",
     "v10-cooldown-pill", "v10-cooldown-pill-divider",
    ].forEach(function (id) {
      var _el = document.getElementById(id);
      if (!_el) return;
      // The dividers before VIX and Day are always shown; hide the rest.
      var _isDivider = id.indexOf("divider") !== -1;
      if (!_showGatePills) {
        _el.style.display = "none";
      }
      // elements will be re-shown below if _showGatePills is true
    });
    // Add / remove a CLOSED chip next to the mode pill when off-hours.
    var _closedChip = document.getElementById("v10-closed-chip");
    if (!_showGatePills) {
      if (!_closedChip) {
        _closedChip = document.createElement("span");
        _closedChip.id = "v10-closed-chip";
        _closedChip.style.cssText = "padding:3px 10px;border-radius:999px;font-size:12px;"
          + "font-weight:600;background:#374151;color:#9ca3af;";
        _closedChip.title = "Market closed — session gates reset at 09:25 ET";
        var _v10Banner = document.getElementById("v10-day-status");
        if (_v10Banner) _v10Banner.appendChild(_closedChip);
      }
      _closedChip.textContent = "CLOSED";
      _closedChip.style.display = "";
      return; // skip all gate pill rendering below
    }
    if (_closedChip) _closedChip.style.display = "none";

    var vixEl = document.getElementById("v10-vix");
    var vixPassEl = document.getElementById("v10-vix-pass");
    var dayStateEl = document.getElementById("v10-day-state");
    var thr = ds.vix_threshold || 22.0;
    // v9.1.31 -- vix_d1_close is Alpaca's prior-day VIX (often null
    // because Alpaca equity feed doesn't cover VIX). Fall back to
    // vix_current (Yahoo ^VIX injected by dashboard_server.py) for
    // display; gate evaluation is still based on vix_d1_close.
    var vix = ds.vix_d1_close;
    var vixCurrent = ds.vix_current;
    var vixDisplay = (vix != null) ? vix : vixCurrent;
    var vixIsLive = (vix == null && vixCurrent != null);
    if (vixEl) {
      var vixNumStr = vixDisplay != null ? vixDisplay.toFixed(2) : "n/a";
      vixEl.textContent = vixNumStr + "/" + thr.toFixed(0);
      vixEl.title = vixIsLive
        ? "Current VIX " + vixNumStr + " (live Yahoo; prior-day close unavailable from Alpaca equity feed)"
        : "Prior-day VIX close " + vixNumStr;
    }
    if (vixPassEl) {
      // Gate uses prior-day close; if unavailable show "?" with
      // a note that current VIX is within / outside the threshold.
      var pass = (vix == null)
        ? (vixCurrent != null ? (vixCurrent > thr ? "WARN" : "OK") : "?")
        : (vix > thr ? "FAIL" : "PASS");
      vixPassEl.textContent = pass;
      if (pass === "PASS" || pass === "OK") {
        vixPassEl.style.background = "#16a34a"; vixPassEl.style.color = "#fff";
      } else if (pass === "FAIL") {
        vixPassEl.style.background = "#dc2626"; vixPassEl.style.color = "#fff";
      } else if (pass === "WARN") {
        vixPassEl.style.background = "rgba(245,158,11,0.25)"; vixPassEl.style.color = "#fbbf24";
      } else {
        vixPassEl.style.background = "#374151"; vixPassEl.style.color = "#e5e7eb";
      }
      if (vixIsLive) vixPassEl.title = "Based on current VIX — prior-day close unavailable from Alpaca";
    }
    if (dayStateEl) {
      if (ds.block_day) {
        dayStateEl.textContent = "BLOCKED (" + (ds.block_reason || "?") + ")";
        dayStateEl.style.color = "#dc2626";
      } else {
        dayStateEl.textContent = "OK";
        dayStateEl.style.color = "#22c55e";
      }
    }

    // v8.1.2 -- ATR-stop + Partial-profit chips. Read from
    // v10.config which the engine populates via snapshot(). Hide
    // the chip entirely when the corresponding feature is off so
    // operators only see what's actually active.
    var cfg = (v10 && v10.config) || {};
    var atrMult = parseFloat(cfg.atr_stop_mult || 0);
    var atrPill = document.getElementById("v10-atr-pill");
    var atrDiv = document.getElementById("v10-atr-pill-divider");
    if (atrPill && atrDiv) {
      if (atrMult > 0) {
        atrPill.textContent = "ATR×" + atrMult.toFixed(2);
        atrPill.title = "ATR-based stop placement: stop = entry ∓ "
          + atrMult.toFixed(2) + " × ATR(" + (cfg.atr_lookback_5m || 14)
          + ", 5m). Cold-ATR falls back to OR-edge silently. v8.0.0+.";
        atrPill.style.display = "";
        atrDiv.style.display = "";
      } else {
        atrPill.style.display = "none";
        atrDiv.style.display = "none";
      }
    }
    // v8.1.8 -- wash-sale risk counter chip. Hidden when count=0
    // so the banner stays uncluttered on clean-trading days.
    var washN = parseInt(v10.wash_risk_count || 0, 10) || 0;
    var washPill = document.getElementById("v10-wash-pill");
    var washDiv = document.getElementById("v10-wash-pill-divider");
    if (washPill && washDiv) {
      if (washN > 0) {
        washPill.textContent = "Wash " + washN;
        washPill.title = washN + " entr"
          + (washN === 1 ? "y" : "ies")
          + " this session re-opened a (ticker, side) within 30 "
          + "days of a losing close. The IRS §1091 wash-sale rule "
          + "may defer the loss for tax purposes. Strategy is "
          + "unblocked -- this is operator visibility only. "
          + "Most active intraday traders elect §475(f) MTM to "
          + "exempt themselves from §1091.";
        washPill.style.display = "";
        washDiv.style.display = "";
      } else {
        washPill.style.display = "none";
        washDiv.style.display = "none";
      }
    }

    // v9.0.0 -- prior-day SPY regime pill. Shows SPY(D-1) close-to-close
    // return + threshold + pass/block status. Hidden when feature is
    // off (threshold = 0).
    var spyThr = parseFloat(ds.spy_threshold_bps || 0);
    var spyRet = ds.spy_d1_ret_bps;
    var spyPill = document.getElementById("v10-spy-pill");
    var spyDiv = document.getElementById("v10-spy-pill-divider");
    if (spyPill && spyDiv) {
      if (spyThr !== 0) {
        var spyText, spyBg, spyFg, spyPass;
        if (spyRet == null) {
          spyText = "SPY n/a"; spyBg = "#374151"; spyFg = "#e5e7eb"; spyPass = "missing";
        } else {
          var retPct = (spyRet / 100).toFixed(2);
          spyPass = spyRet < spyThr ? "BLOCK" : "PASS";
          spyText = "SPY " + (spyRet >= 0 ? "+" : "") + retPct + "%";
          if (spyPass === "BLOCK") { spyBg = "rgba(220,38,38,0.18)"; spyFg = "#fca5a5"; }
          else { spyBg = "rgba(22,163,74,0.18)"; spyFg = "#86efac"; }
        }
        spyPill.textContent = spyText + " · " + spyPass;
        spyPill.style.background = spyBg;
        spyPill.style.color = spyFg;
        spyPill.title = "Prior-session SPY close-to-close return. "
          + "Threshold " + (spyThr / 100).toFixed(2)
          + "% (v9.0.0 regime gate). Day is blocked when prior SPY "
          + "return is below threshold (R12 backtest: bleed concentrated "
          + "on moderate-down-day carryover).";
        spyPill.style.display = "";
        spyDiv.style.display = "";
      } else {
        spyPill.style.display = "none";
        spyDiv.style.display = "none";
      }
    }

    // v9.0.0 -- min_break rejection counter. Hidden when count=0.
    var mbrN = parseInt(v10.mbr_reject_count || 0, 10) || 0;
    var mbrPill = document.getElementById("v10-mbr-pill");
    var mbrDiv = document.getElementById("v10-mbr-pill-divider");
    if (mbrPill && mbrDiv) {
      if (mbrN > 0) {
        var mbrThr = parseFloat(cfg.min_break_bps || 0);
        mbrPill.textContent = "mbr " + mbrN;
        mbrPill.title = mbrN + " entr" + (mbrN === 1 ? "y" : "ies")
          + " rejected this session because the signal-bar close was "
          + "within " + mbrThr.toFixed(0) + "bps of the OR boundary "
          + "(weak breakout). Threshold set by ORB_MIN_BREAK_BPS. "
          + "v9.0.0+.";
        mbrPill.style.display = "";
        mbrDiv.style.display = "";
      } else {
        mbrPill.style.display = "none";
        mbrDiv.style.display = "none";
      }
    }

    // v9.0.0 -- vwap-chase rejection counter (mega-cap fence). Hidden
    // when count=0.
    var chaseN = parseInt(v10.vwap_chase_reject_count || 0, 10) || 0;
    var chasePill = document.getElementById("v10-chase-pill");
    var chaseDiv = document.getElementById("v10-chase-pill-divider");
    if (chasePill && chaseDiv) {
      if (chaseN > 0) {
        var chaseThr = parseFloat(cfg.max_vwap_dev_bps || 0);
        var fence = cfg.max_vwap_dev_tickers || [];
        chasePill.textContent = "chase " + chaseN;
        chasePill.title = chaseN + " entr" + (chaseN === 1 ? "y" : "ies")
          + " rejected this session because the entry price was more "
          + "than " + chaseThr.toFixed(0) + "bps past session VWAP in "
          + "the breakout direction. "
          + (fence.length
              ? "Fence applies to: " + fence.join(", ") + ". "
              : "Filter applies globally. ")
          + "v9.0.0 chase-prevention (R10 backtest winner).";
        chasePill.style.display = "";
        chaseDiv.style.display = "";
      } else {
        chasePill.style.display = "none";
        chaseDiv.style.display = "none";
      }
    }

    var partialOn = !!cfg.partial_profit_at_1r;
    var pPill = document.getElementById("v10-partial-pill");
    var pDiv = document.getElementById("v10-partial-pill-divider");
    if (pPill && pDiv) {
      if (partialOn) {
        pPill.textContent = "P@1R ON";
        pPill.style.background = "#166534"; // green-700
        pPill.style.color = "#dcfce7";       // green-100
        pPill.title = "Partial-profit-at-1R is ACTIVE. Engine emits "
          + "EXIT_PARTIAL on first 1R touch; broker submits MARKET "
          + "half-sell; runner rides to 2.5R with BE stop. v8.1.0+.";
        pPill.style.display = "";
        pDiv.style.display = "";
      } else {
        // Render greyed when off so operator can confirm the
        // env flag state at a glance (vs no chip at all, which
        // is ambiguous between "off" and "not deployed yet").
        pPill.textContent = "P@1R OFF";
        pPill.style.background = "#374151"; // gray-700
        pPill.style.color = "#9ca3af";       // gray-400
        pPill.title = "Partial-profit-at-1R env flag is unset/0. "
          + "Set ORB_PARTIAL_PROFIT_AT_1R=1 in Railway to activate. "
          + "v8.1.0+.";
        pPill.style.display = "";
        pDiv.style.display = "";
      }
    }

    // Trades + risk used.
    // v7.50.0 -- prefer the backend-injected broker_trades_today so
    // the count matches what each pid's Alpaca account actually
    // booked (mirror mode keeps v10's trades_today=0 for val/gene).
    // v7.57.0 -- when pidFilter is set, scope everything to that pid.
    function _tradesForDs(d) {
      return (d.broker_trades_today != null)
              ? d.broker_trades_today
              : (d.trades_today || 0);
    }
    var dayStates = v10.day_states || [];
    var rb = v10.risk_books || {};
    var pidsAll = Object.keys(rb).sort();
    var pidsScope = pidFilter ? [pidFilter] : pidsAll;

    // Per-pid + per-ticker trade counts (used by both gauge math and
    // the per-pid strip below).
    // v7.63.0 -- fix double-counting bug. The backend writes
    // broker_trades_today (book total) redundantly onto every
    // day_state row of a pid, so summing across rows multiplied the
    // total by the ticker count (e.g. main with 3 trades on AAPL+NVDA
    // was being displayed as 6). Now:
    //   - perPidTrades[pid]: take broker_trades_today ONCE per pid
    //     (authoritative book total); fall back to summing v10
    //     trades_today across this pid's rows when broker_* is absent.
    //   - maxTickerPerPid[pid]: always use the per-ticker v10
    //     trades_today; broker_trades_today is a book total, not a
    //     per-ticker count, so using it for "top ticker" was a
    //     second bug.
    var perPidTrades = {};
    var maxTickerPerPid = {};
    var brokerSeenForPid = {};   // pid -> have we captured broker_trades_today yet?
    for (var j = 0; j < dayStates.length; j++) {
      var ds = dayStates[j];
      var p = ds.portfolio_id || "?";
      var vt = ds.trades_today || 0;
      if (ds.broker_trades_today != null) {
        if (!brokerSeenForPid[p]) {
          perPidTrades[p] = ds.broker_trades_today;
          brokerSeenForPid[p] = true;
        }
      } else {
        // No broker hint -- accumulate per-ticker v10 counts.
        perPidTrades[p] = (perPidTrades[p] || 0) + vt;
      }
      if (vt > (maxTickerPerPid[p] || 0)) maxTickerPerPid[p] = vt;
    }
    var maxTrades = (v10.config && v10.config.max_trades_per_day) || 5;

    // Scoped sums.
    var scopedTrades = 0;
    var scopedMaxTicker = 0;
    pidsScope.forEach(function (pid) {
      scopedTrades += (perPidTrades[pid] || 0);
      if ((maxTickerPerPid[pid] || 0) > scopedMaxTicker) {
        scopedMaxTicker = maxTickerPerPid[pid];
      }
    });

    var tradesEl = document.getElementById("v10-trades-used");
    if (tradesEl) {
      // For the header text we want the operator to see a clear number
      // with the per-ticker cap in plain sight. v7.57.0 -- previous
      // "8/15" form misled into "8 of 15 portfolio-level trades"; the
      // cap is actually per (ticker, portfolio, day). Show book total
      // + per-ticker cap explicitly.
      tradesEl.textContent = scopedTrades + " today";
      tradesEl.title = "Total trades today on " +
        (pidFilter ? pidFilter : "all books") +
        ". Cap: " + maxTrades + " entries per ticker per day "
        + "(top ticker so far today: " + scopedMaxTicker + "/" + maxTrades + ").";
    }

    var riskUsed = 0;
    var riskMax = 0;
    pidsScope.forEach(function (pid) {
      riskUsed += (rb[pid] && rb[pid].open_risk) || 0;
      riskMax += (rb[pid] && rb[pid].max_risk_dollars) || 0;
    });
    var riskEl = document.getElementById("v10-risk-used");
    if (riskEl) {
      riskEl.textContent = "$" + Math.round(riskUsed) + " / $" + Math.round(riskMax);
      if (pidFilter) {
        riskEl.title = pidFilter + " open risk vs cap. New entries are "
          + "rejected with reason=risk_cap when (open_risk + new_trade) > cap.";
      } else {
        var riskTip = pidsScope.map(function (p) {
          return p + ":$" + Math.round((rb[p] && rb[p].open_risk) || 0)
            + "/$" + Math.round((rb[p] && rb[p].max_risk_dollars) || 0);
        }).join("  ");
        riskEl.title = riskTip;
      }
    }

    // v7.41.0: gauges row -- visual bars for Trades / Risk / Daily-kill.
    var gaugesRow = document.getElementById("v10-gauges-row");
    if (!gaugesRow) {
      gaugesRow = document.createElement("div");
      gaugesRow.id = "v10-gauges-row";
      gaugesRow.className = "v10-gauges-row";
      banner.appendChild(gaugesRow);
    }
    // v7.57.0 -- trades gauge fills on the highest-trades-on-any-ticker
    // for this scope vs the per-ticker cap, because the cap is per
    // (ticker, portfolio, day) -- not per book. Filling on book-total
    // / N*cap was the misleading framing the operator flagged.
    var tradesPct = maxTrades > 0 ? (scopedMaxTicker / maxTrades * 100) : 0;
    var riskPct = riskMax > 0 ? (riskUsed / riskMax * 100) : 0;
    // Worst-pid daily-kill % drives the kill gauge -- closest-to-
    // blown is what an operator needs to see at a glance.
    var killWorstPct = 0;
    var killWorstPid = null;
    var killWorstRealized = 0;
    var killWorstThreshold = 0;
    var anyKillTriggered = false;
    // v9.1.4 -- track the worst pid even when no loss has happened yet
    // so the gauge stays visible at $0 framing. Pre-v9.1.4 Main only
    // rendered the gauge once realized P&L went negative, while
    // Val/Gene's renderV10PerPortfolio always rendered when the
    // threshold was configured -- visible cross-tab parity break.
    pidsScope.forEach(function (pid) {
      var b = rb[pid] || {};
      var thr = b.daily_kill_threshold || 0;
      var realized = b.realized_pnl_today || 0;
      if (thr > 0) {
        var pct = realized < 0 ? Math.abs(realized) / thr * 100 : 0;
        // Prefer the pid with the highest loss pct. If no losses yet,
        // fall back to the first pid with a configured threshold so the
        // gauge stays on screen with empty $0 / -$X framing.
        if (pct > killWorstPct ||
            (killWorstPid === null && pct === 0)) {
          killWorstPct = pct;
          killWorstPid = pid;
          killWorstRealized = realized;
          killWorstThreshold = thr;
        }
      }
      if (b.daily_kill_triggered) anyKillTriggered = true;
    });

    function _gaugeHtml(label, value, fillPct, cls) {
      var clamped = Math.max(0, Math.min(110, fillPct));
      var stateCls = '';
      if (fillPct >= 90 || cls === 'danger' || (cls || '').indexOf('danger') >= 0) stateCls = ' v10-gauge-danger';
      else if (fillPct >= 70) stateCls = ' v10-gauge-warn';
      return '<div class="v10-gauge ' + (cls || '') + stateCls + '">'
           + '<div class="v10-gauge-head">'
           + '<span class="v10-gauge-label">' + label + '</span>'
           + '<span class="v10-gauge-value">' + value + '</span>'
           + '</div>'
           + '<div class="v10-gauge-bar">'
           + '<div class="v10-gauge-fill" style="width:'
                + clamped.toFixed(1) + '%"></div>'
           + '</div></div>';
    }

    // v7.57.0 -- trades gauge re-framed. The cap is PER TICKER, so the
    // fill is "top ticker N/5" and the value text shows both the book
    // total and the per-ticker high-water mark with explicit "ticker"
    // wording so it's unambiguous.
    var html = '';
    html += _gaugeHtml(
      'Trades today (cap 5/ticker)',
      scopedTrades + ' total · top ticker ' +
        scopedMaxTicker + '/' + maxTrades,
      tradesPct
    );
    html += _gaugeHtml(
      'Concurrent risk',
      '$' + Math.round(riskUsed).toLocaleString()
       + ' / $' + Math.round(riskMax).toLocaleString()
       + ' (' + riskPct.toFixed(0) + '%)',
      riskPct
    );
    // v9.1.4 -- render whenever ANY pid has a configured threshold,
    // matching Val/Gene's renderV10PerPortfolio gate. Empty gauge
    // shows $0 / -$thr (0%) until a loss actually happens.
    if (killWorstThreshold > 0) {
      // v7.57.0 -- when scoped to one pid, the "worst pid" framing is
      // misleading; just say "Daily-kill" for the single book.
      var killLabel = pidFilter ? 'Daily-kill' : 'Daily-kill (worst pid)';
      var killValue = killWorstPid
        ? ((pidFilter ? '' : killWorstPid.toUpperCase() + ' ') + '$' +
           Math.round(killWorstRealized).toLocaleString() +
           ' / -$' + Math.round(killWorstThreshold).toLocaleString())
        : 'no kill data';
      var killCls = 'v10-gauge-kill' +
                    (anyKillTriggered ? ' danger' : '');
      html += _gaugeHtml(killLabel, killValue, killWorstPct, killCls);
    }
    gaugesRow.innerHTML = html;

    // v7.23.0 + v7.41.0: Per-portfolio rows under the gauges. Each
    // row carries a mini daily-kill bar so the operator can spot the
    // closest-to-blown portfolio at a glance.
    // v7.57.0 -- hidden entirely when scoped to a single pid (Main
    // tab now scopes to "main"; showing a 1-row "main / val / gene"
    // strip with two stale rows was the cross-portfolio leakage the
    // operator flagged).
    var perPidStrip = document.getElementById("v10-pid-strip");
    if (!perPidStrip) {
      perPidStrip = document.createElement("div");
      perPidStrip.id = "v10-pid-strip";
      perPidStrip.style.cssText = "display:flex;flex-wrap:wrap;gap:8px;margin-top:6px;width:100%;font-size:11px;font-family:'JetBrains Mono',monospace";
      banner.appendChild(perPidStrip);
    }
    perPidStrip.innerHTML = "";
    if (pidFilter) {
      perPidStrip.style.display = "none";
      return;
    } else {
      perPidStrip.style.display = "flex";
    }
    // v7.52.0 -- per-pid equity should reflect actual portfolio size
    // (real-time NAV), not the v10 RiskBook's `equity` which may lag
    // if refresh_equity_from_books() hasn't run since session boot
    // or is failing silently in production. Priority order:
    //   1. Main:    s.portfolio.equity (the headline KPI value)
    //   2. Val:     /api/executor/val.account.equity (Alpaca-reported)
    //      Gene:    /api/executor/gene.account.equity (Alpaca-reported)
    //   3. Fallback: v10 RiskBook equity (current behaviour)
    function _liveEquityFor(pid) {
      if (pid === "main") {
        var pe = ((s.portfolio || {}).equity);
        if (typeof pe === "number" && pe > 0) return pe;
      } else if (typeof window !== "undefined") {
        // _execLastData lives in the executor-poll IIFE; cross-IIFE
        // read via window for the same reason as the other renderers.
        var cache = window.__tgExecLastData;
        var d = cache && cache[pid];
        var eq = d && d.account && d.account.equity;
        if (typeof eq === "number" && eq > 0) return eq;
      }
      return null;
    }
    pidsAll.forEach(function (pid) {
      var b = rb[pid] || {};
      var row = document.createElement("span");
      row.className = "v10-pid-row";
      var openCount = b.open_count || 0;
      var ut = b.utilization_pct || 0;
      var color = openCount > 0 ? "#fbbf24" : "#374151";
      var pidKillPct = 0;
      if ((b.daily_kill_threshold || 0) > 0 &&
          (b.realized_pnl_today || 0) < 0) {
        pidKillPct = Math.abs(b.realized_pnl_today) /
                     b.daily_kill_threshold * 100;
      }
      var miniGauge =
        '<span class="v10-mini-gauge" title="daily-kill ' +
        pidKillPct.toFixed(0) + '% of threshold">' +
        '<span class="v10-mini-gauge-fill" style="width:' +
        Math.min(100, pidKillPct).toFixed(0) + '%"></span>' +
        '</span>';
      row.style.cssText = "padding:3px 8px;background:#0a0d12;border:1px solid #1f2937;border-radius:6px;color:#9ca3af";
      var liveEq = _liveEquityFor(pid);
      var displayEq = (liveEq != null) ? liveEq : (b.equity || 0);
      row.innerHTML = "<b style=\"color:" + color + "\">" + pid + "</b>"
        + "  $" + Math.round(displayEq).toLocaleString()
        + "  " + (perPidTrades[pid] || 0) + "/" + maxTrades + " trades"
        + "  " + (openCount > 0 ? openCount + " open" : "no open")
        + "  " + ut.toFixed(0) + "% util"
        + (pidKillPct > 0
            ? "  kill" + miniGauge + pidKillPct.toFixed(0) + "%"
            : "");
      perPidStrip.appendChild(row);
    });
  }

  function fmtPct(v, prec) {
    if (v == null) return "—";
    var p = (prec == null) ? 1 : prec;
    return (v >= 0 ? "+" : "") + v.toFixed(p) + "%";
  }
  function fmtMoney(v) {
    if (v == null) return "—";
    return "$" + Math.round(v).toLocaleString();
  }

  // v7.27.0 -- per-ticker FSM/OR matrix sourced from
  // orb.live_runtime.snapshot() (s.v10.day_states + s.v10.or_windows).
  // Renders the canonical v10 view. Hidden when v10 is not bootstrapped;
  // the legacy Permit Matrix below covers the pre-v10 path until the
  // v7.28.0 retirement removes it physically.
  // v7.57.0 -- pidFilter scopes the matrix to a single book so the
  // Main tab only renders main rows, etc. Passing null keeps the
  // legacy cross-portfolio render (no current caller).
  function renderV10TickerMatrix(s, pidFilter) {
    // v9.1.11 -- retired. The Ticker Matrix section was a strict
    // subset of the v10 Proximity (now "v10 Matrix") section; its
    // phase + trade-count signals have been absorbed into the
    // Proximity phase chip ("pid n/cap"). This stub keeps the public
    // window.__tgRenderV10TickerMatrix entry alive as a no-op so any
    // back-compat caller (renderAll) finds it without breaking.
    var section = document.getElementById("v10-ticker-matrix-section");
    if (section) section.style.display = "none";
    return;
  }

  // v7.52.0 -- v10 Proximity Matrix renderer. For each ticker with
  // a locked OR window, shows current price + OR_low + OR_high +
  // the closer of (price -> OR_high) / (OR_low -> price) as a
  // signed distance %. FSM phase per pid (main/val/gene) as mini
  // chips. Sorted by absolute distance (closest-to-break first).
  function renderV10ProximityMatrix(s) {
    var section = document.getElementById("v10-proximity-section");
    if (!section) return;
    // v7.54.0 -- always visible (see comment in _renderV10ProximityCore)
    section.style.display = "";
    // v7.57.0 -- pidFilter="main" so only Main's FSM phase chips show
    // on this card (Main tab is now Main-only across all v10 surfaces).
    _renderV10ProximityCore(s, {
      body: document.getElementById("v10-prox-body"),
      countEl: document.getElementById("v10-prox-count"),
      summaryEl: document.getElementById("v10-prox-summary"),
      expandedKey: "main",
      pidFilter: "main",
      rerender: function () { renderV10ProximityMatrix(s); },
    });
  }

  // v7.55.0 -- per-portfolio variant. Renders the same proximity card
  // into the Val/Gene exec panel section added in v7.55.0
  // (data-f="v10-prox-section-pid"). Phase chips are filtered to just
  // the panel's pid so the card stays focused on what THIS book is
  // doing. OR data + distance are market-wide so they're shared.
  function renderV10ProximityForPanel(s, panel, pid) {
    if (!panel || !pid) return;
    var section = panel.querySelector('[data-f="v10-prox-section-pid"]');
    if (!section) return;
    section.style.display = "";
    _renderV10ProximityCore(s, {
      body: panel.querySelector('[data-f="v10-prox-pid-body"]'),
      countEl: panel.querySelector('[data-f="v10-prox-pid-count"]'),
      summaryEl: panel.querySelector('[data-f="v10-prox-pid-summary"]'),
      expandedKey: "panel-" + pid,
      pidFilter: pid,
      rerender: function () { renderV10ProximityForPanel(s, panel, pid); },
    });
  }

  // v7.55.0 -- shared row builder + DOM writer extracted out of
  // renderV10ProximityMatrix so the per-pid panels (Val/Gene) can reuse
  // the same logic with a different target body and an optional pid
  // filter on the phase chips.
  function _renderV10ProximityCore(s, opts) {
    function esc(v) {
      return String(v == null ? "" : v)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;")
        .replace(/>/g, "&gt;").replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
    }
    var v10 = (s && s.v10) || {};
    var body = opts.body;
    var countEl = opts.countEl;
    var summaryEl = opts.summaryEl;
    var orWindows = v10.or_windows || {};
    var dayStates = v10.day_states || [];
    var prices = v10.prices || {};
    var cfg = v10.config || {};
    // v7.59.0 -- leading-indicator data: OR-width admissibility +
    // per-ticker blocklist + per-(pid,ticker) block_reason.
    var rangeMin = (typeof cfg.range_min_pct === "number") ? cfg.range_min_pct : null;
    var rangeMax = (typeof cfg.range_max_pct === "number") ? cfg.range_max_pct : null;
    var blocklist = cfg.blocklist || {};
    var dayStatusBlock = (v10.day_status && v10.day_status.block_day)
      ? (v10.day_status.block_reason || "day_block") : null;

    // Index FSM phases + block_reason + per-ticker trade count by
    // ticker -> { pid -> {phase, block_reason, trades_today} }
    // v9.1.11 -- trades_today carried through so the phase chip can
    // surface the per-(ticker,pid) trade-cap usage. Pre-v9.1.11 that
    // info lived in a separate "v10 Ticker Matrix" section that
    // showed the exact same FSM phases as Proximity does -- a
    // strict subset duplicated as a parallel section. Removing the
    // ticker matrix and merging the trade-count signal into the
    // phase chip keeps the data without the duplication.
    var phaseByTk = {};
    for (var i = 0; i < dayStates.length; i++) {
      var d = dayStates[i];
      if (!d.ticker) continue;
      if (!phaseByTk[d.ticker]) phaseByTk[d.ticker] = {};
      phaseByTk[d.ticker][d.portfolio_id || "?"] = {
        phase: d.phase || "?",
        block_reason: d.block_reason || "",
        trades_today: (typeof d.trades_today === "number") ? d.trades_today : 0,
      };
    }
    var maxTrades = (cfg && cfg.max_trades_per_day) || 5;

    // Universe: union of (a) tickers in or_windows, (b) tickers in
    // day_states, and (c) s.tickers (the configured universe). Order
    // by closer-to-break first when proximity is computable, then
    // alphabetical for the rest.
    var universe = {};
    Object.keys(orWindows).forEach(function (t) { universe[t] = 1; });
    Object.keys(phaseByTk).forEach(function (t) { universe[t] = 1; });
    if (Array.isArray(s.tickers)) {
      s.tickers.forEach(function (t) { if (t) universe[t] = 1; });
    }

    var rows = [];
    Object.keys(universe).forEach(function (tkr) {
      var w = orWindows[tkr] || {};
      // Current price: prefer or_window.current_price (set when ticker
      // is in the v10 universe), fall back to v10.prices map (added in
      // v7.54.0 for the full universe).
      var px = (typeof w.current_price === "number") ? w.current_price
                : (typeof prices[tkr] === "number" ? prices[tkr] : null);
      var locked = !!w.locked;
      var orh = (locked && typeof w.or_high === "number") ? w.or_high : null;
      var orl = (locked && typeof w.or_low === "number")  ? w.or_low  : null;

      // distance to break -- only meaningful when OR has locked.
      var distToHigh = (locked && px != null && orh != null && orh > 0)
                        ? ((px - orh) / orh * 100) : null;
      var distToLow  = (locked && px != null && orl != null && orl > 0)
                        ? ((px - orl) / orl * 100) : null;
      var closer = null;
      var closerLabel = "";
      if (distToHigh != null && distToLow != null) {
        if (Math.abs(distToHigh) <= Math.abs(distToLow)) {
          closer = distToHigh; closerLabel = "OR-high";
        } else {
          closer = distToLow;  closerLabel = "OR-low";
        }
      } else if (distToHigh != null) { closer = distToHigh; closerLabel = "OR-high"; }
      else if (distToLow  != null) { closer = distToLow;  closerLabel = "OR-low"; }

      // v7.59.0 -- leading-indicator data per row.
      // Range admissibility: OR width must fall inside [range_min, range_max].
      var widthPct = locked ? w.or_width_pct : null;
      var rangeOk = null;
      var rangeNote = "";
      if (locked && typeof widthPct === "number" && rangeMin != null && rangeMax != null) {
        rangeOk = (widthPct >= rangeMin && widthPct <= rangeMax);
        if (!rangeOk) {
          rangeNote = widthPct < rangeMin
            ? "too tight (" + (widthPct * 100).toFixed(2) + "% < " + (rangeMin * 100).toFixed(2) + "%)"
            : "too wide (" + (widthPct * 100).toFixed(2) + "% > " + (rangeMax * 100).toFixed(2) + "%)";
        }
      }
      // Blocklist (per side, from config).
      var bl = blocklist[tkr] || [];
      var blLong = bl.indexOf && (bl.indexOf("LONG") >= 0 || bl.indexOf("long") >= 0);
      var blShort = bl.indexOf && (bl.indexOf("SHORT") >= 0 || bl.indexOf("short") >= 0);

      rows.push({
        tkr: tkr, px: px, orh: orh, orl: orl, locked: locked,
        or_width_pct: widthPct,
        closer: closer, closer_label: closerLabel,
        phases: phaseByTk[tkr] || {},
        range_ok: rangeOk,
        range_note: rangeNote,
        bl_long: !!blLong,
        bl_short: !!blShort,
      });
    });

    // Sort: locked rows first (closest-to-break ascending), then
    // unlocked alphabetical.
    rows.sort(function (a, b) {
      if (a.locked !== b.locked) return a.locked ? -1 : 1;
      if (a.locked && b.locked) {
        var aa = (a.closer == null) ? 1e9 : Math.abs(a.closer);
        var bb = (b.closer == null) ? 1e9 : Math.abs(b.closer);
        return aa - bb;
      }
      return a.tkr < b.tkr ? -1 : (a.tkr > b.tkr ? 1 : 0);
    });

    var lockedCount = rows.filter(function (r) { return r.locked; }).length;
    if (countEl) countEl.textContent = "· " + lockedCount + " / " + rows.length + " locked";
    if (summaryEl) {
      if (rows.length === 0) {
        summaryEl.textContent = "universe empty";
      } else if (lockedCount === 0) {
        summaryEl.textContent = "OR window not locked yet";
      } else {
        var top = rows[0];
        var dd = (top.closer == null) ? "" :
          ((top.closer >= 0 ? "+" : "") + top.closer.toFixed(2) + "%");
        summaryEl.textContent = top.tkr + " " + dd + " from " + top.closer_label;
      }
    }
    if (!body) return;
    if (rows.length === 0) {
      body.innerHTML = '<div class="empty">Universe is empty -- check TRADE_TICKERS configuration.</div>';
      return;
    }

    function _phaseChip(pid, phaseInfo) {
      // v7.59.0 -- phaseInfo carries {phase, block_reason}.
      // v9.1.11 -- now also {trades_today}. Chip body shows pid + the
      // per-ticker trade count "pid n/cap" so the operator gets the
      // trade-cap utilization at a glance (replaces the separate v10
      // Ticker Matrix section). Tooltip still surfaces phase + reason.
      var phase = (phaseInfo && phaseInfo.phase) || "?";
      var reason = (phaseInfo && phaseInfo.block_reason) || "";
      var trades = (phaseInfo && typeof phaseInfo.trades_today === "number")
                     ? phaseInfo.trades_today : 0;
      var cls = "v10-prox-phase v10-prox-phase-" + phase.toLowerCase();
      var titleTxt = pid + ": " + phase
                       + " (" + trades + "/" + maxTrades + " trades today)"
                       + (reason ? " — " + reason : "");
      var label = pid + " " + trades + "/" + maxTrades;
      return '<span class="' + cls + '" title="' + esc(titleTxt) + '">'
           + esc(label) + '</span>';
    }

    // v7.59.0 -- compact ✓/✕ cell for the Range column.
    function _rangeCell(rangeOk, rangeNote, locked) {
      if (!locked) return '<span class="v10-prox-gate v10-prox-gate-pending" title="OR window not locked">—</span>';
      if (rangeOk === null) return '<span class="v10-prox-gate">—</span>';
      var icon = rangeOk ? "✓" : "✕";
      var clsExtra = rangeOk ? " v10-prox-gate-pass" : " v10-prox-gate-fail";
      var tip = rangeOk
        ? "Range OK: OR width within admissible band"
        : "Range fail: " + rangeNote;
      return '<span class="v10-prox-gate' + clsExtra + '" title="' + esc(tip) + '">' + icon + '</span>';
    }

    // v7.59.0 -- compact Block summary. Reports per-side blocklist
    // entries + per-pid block_reason. Single-pid panels (Val/Gene)
    // restrict the reason scan to that pid.
    function _blockCell(r, pidFilter) {
      var bits = [];
      if (r.bl_long) bits.push('<span class="v10-prox-block-chip v10-prox-block-side" title="Blocklist: LONG side disabled for this ticker">L blk</span>');
      if (r.bl_short) bits.push('<span class="v10-prox-block-chip v10-prox-block-side" title="Blocklist: SHORT side disabled for this ticker">S blk</span>');
      // Day-status block applies to ALL tickers; surface it once per row.
      if (dayStatusBlock) {
        bits.push('<span class="v10-prox-block-chip v10-prox-block-day" title="Day-level block: ' + esc(dayStatusBlock) + '">day</span>');
      }
      // Per-pid block_reason -- show when at least one phase chip is BLOCKED_*.
      var pidsToScan = pidFilter ? [pidFilter] : ["main", "val", "gene"];
      var reasons = [];
      pidsToScan.forEach(function (p) {
        var pi = r.phases[p];
        if (!pi) return;
        if ((pi.phase || "").toLowerCase().indexOf("blocked") !== 0) return;
        var rsn = (pi.block_reason || pi.phase || "").trim();
        if (rsn && reasons.indexOf(rsn) < 0) reasons.push(rsn);
      });
      reasons.forEach(function (rs) {
        bits.push('<span class="v10-prox-block-chip v10-prox-block-reason" title="Block reason: ' + esc(rs) + '">' + esc(rs.split(" ")[0]) + '</span>');
      });
      if (bits.length === 0) return '<span class="v10-prox-gate-pass" title="No blocks">✓</span>';
      return bits.join(" ");
    }

    function _distCell(closer, label) {
      if (closer == null) return '<span class="v10-prox-dist">—</span>';
      var sign = closer >= 0 ? "+" : "";
      var cls = "v10-prox-dist";
      var absD = Math.abs(closer);
      if (absD < 0.3) cls += " v10-prox-near";
      else if (absD < 1.0) cls += " v10-prox-mid";
      else cls += " v10-prox-far";
      // Direction arrow: above OR_high (+) means already broke up;
      // below OR_low (-) means already broke down. Inside the window
      // shows the relative position.
      var arrow = "";
      if (label === "OR-high") arrow = (closer >= 0) ? "↑" : "↗";
      else if (label === "OR-low") arrow = (closer < 0) ? "↓" : "↘";
      return '<span class="' + cls + '">' + arrow + " " + sign + absD.toFixed(2) + "% "
           + '<span class="v10-prox-dist-label">' + esc(label) + '</span></span>';
    }

    // v7.53.0 -- track which tickers are expanded across renders so the
    // table can be rebuilt every state tick without collapsing the
    // operator's open detail rows. Module-level + keyed by panel
    // (Main / Val / Gene) so each panel's expansion state is
    // independent. Resets on page refresh.
    if (!_renderV10ProximityCore._expanded) {
      _renderV10ProximityCore._expanded = {};
    }
    var expandedKey = opts.expandedKey || "main";
    if (!_renderV10ProximityCore._expanded[expandedKey]) {
      _renderV10ProximityCore._expanded[expandedKey] = {};
    }
    var expanded = _renderV10ProximityCore._expanded[expandedKey];

    var html = '<div class="v10-prox-table-wrap"><table class="v10-prox-table">';
    html += '<thead><tr>'
         +    '<th></th>'
         +    '<th>Ticker</th>'
         +    '<th title="Last traded price">Last</th>'
         +    '<th title="Opening-range LOW">OR-low</th>'
         +    '<th title="Opening-range HIGH">OR-high</th>'
         +    '<th title="OR window width (high - low) as % of mid">Width</th>'
         +    '<th title="OR width vs the admissible range filter [range_min_pct, range_max_pct]. ✓ = admissible. ✕ = too tight or too wide; the breakout EV degrades outside this band.">Range</th>'
         +    '<th title="Distance to the closer break level (signed %)">Distance</th>'
         +    '<th title="Per-portfolio FSM phase">Phase</th>'
         +    '<th title="Entry gates: per-side blocklist + per-pid block_reason. ✓ = nothing blocking new entries on this ticker.">Block</th>'
         +    '</tr></thead><tbody>';
    rows.forEach(function (r) {
      var widthPct = (typeof r.or_width_pct === "number")
                      ? (r.or_width_pct * 100).toFixed(2) + "%" : "—";
      // v7.55.0 -- pidFilter restricts the chip list to a single pid
      // for the per-portfolio panels (Val/Gene). Main passes null and
      // gets all three.
      var pids = opts.pidFilter ? [opts.pidFilter] : ["main", "val", "gene"];
      var chips = pids
        .filter(function (p) { return r.phases[p]; })
        .map(function (p) { return _phaseChip(p, r.phases[p]); })
        .join("");
      var isOpen = !!expanded[r.tkr];
      var caret = isOpen ? "▼" : "▶";
      var openCls = isOpen ? " v10-prox-row-open" : "";
      var unlockedCls = r.locked ? "" : " v10-prox-row-unlocked";
      // v7.54.0 -- when OR is not locked the OR cells are empty and the
      // distance cell shows a "OR pending" pill instead of a number.
      var orLowCell  = r.locked ? r.orl.toFixed(2) : "—";
      var orHighCell = r.locked ? r.orh.toFixed(2) : "—";
      var distHtml = r.locked
                       ? _distCell(r.closer, r.closer_label)
                       : '<span class="v10-prox-pending" title="Opening-range window has not locked yet (locks at the end of OR_minutes after 09:30 ET)">OR pending</span>';
      var rangeHtml = _rangeCell(r.range_ok, r.range_note, r.locked);
      var blockHtml = _blockCell(r, opts.pidFilter);
      html += '<tr class="v10-prox-row' + openCls + unlockedCls + '" data-prox-ticker="' + esc(r.tkr) + '" '
           +  'title="Click to ' + (isOpen ? 'hide' : 'show') + ' intraday chart">'
           +   '<td class="v10-prox-caret">' + caret + '</td>'
           +   '<td class="v10-prox-tkr">' + esc(r.tkr) + '</td>'
           +   '<td class="mono">' + (r.px != null ? r.px.toFixed(2) : "—") + '</td>'
           +   '<td class="mono">' + orLowCell + '</td>'
           +   '<td class="mono">' + orHighCell + '</td>'
           +   '<td class="mono">' + widthPct + '</td>'
           +   '<td class="v10-prox-col-gate">' + rangeHtml + '</td>'
           +   '<td>' + distHtml + '</td>'
           +   '<td>' + chips + '</td>'
           +   '<td class="v10-prox-col-block">' + blockHtml + '</td>'
           + '</tr>';
      if (isOpen) {
        html += '<tr class="v10-prox-detail-row" data-prox-detail="' + esc(r.tkr) + '">'
             +   '<td colspan="10" class="v10-prox-detail-cell">'
             +     '<div class="v10-prox-chart-mount" data-chart-mount="' + esc(r.tkr) + '"></div>'
             +   '</td>'
             + '</tr>';
      }
    });
    html += '</tbody></table></div>';
    body.innerHTML = html;

    // Hydrate every open chart mount via the legacy intraday pipeline
    // exposed by IIFE 1 at window.__tgRenderTickerChart. The cache
    // inside _pmtxHydrateIntradayCharts keys by ticker so a re-render
    // mid-fetch reuses the in-flight payload instead of double-fetching.
    var mountFn = (typeof window !== "undefined") && window.__tgRenderTickerChart;
    if (typeof mountFn === "function") {
      body.querySelectorAll('[data-chart-mount]').forEach(function (mount) {
        var tk = mount.getAttribute("data-chart-mount");
        if (tk) mountFn(tk, mount);
      });
    }

    // Event delegation for row clicks. Re-attached on every render
    // (innerHTML wipes prior listeners). Single listener on body to
    // keep this O(1) attach.
    body.onclick = function (ev) {
      var tr = ev.target && ev.target.closest && ev.target.closest("tr.v10-prox-row");
      if (!tr) return;
      var tk = tr.getAttribute("data-prox-ticker");
      if (!tk) return;
      if (expanded[tk]) { delete expanded[tk]; }
      else { expanded[tk] = true; }
      // Re-render via the panel-specific callback.
      if (typeof opts.rerender === "function") opts.rerender();
    };
  }

  // v7.45.0 -- recent activity feed renderer. Reads s.v10.activity
  // (populated by orb.live_runtime._recent_activity ring buffer).
  // Renders newest-first as a list of rows with colored kind chips.
  // v7.57.0 -- pidFilter scopes events to a single book so the Main
  // tab's activity feed doesn't show val/gene events. Null keeps the
  // legacy cross-book stream (no current caller).
  function renderV10ActivityFeed(s, pidFilter) {
    function esc(v) {
      return String(v == null ? "" : v)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;")
        .replace(/>/g, "&gt;").replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
    }
    var v10 = s && s.v10;
    var section = document.getElementById("v10-activity-section");
    if (!section) return;
    if (!v10 || v10.available === false || !v10.bootstrapped) {
      section.style.display = "none";
      return;
    }
    section.style.display = "";
    var events = (v10.activity || []);
    if (pidFilter) {
      events = events.filter(function (ev) {
        return (ev.pid || "").toLowerCase() === pidFilter;
      });
    }
    // v8.3.16 -- suppress same-tick opposite_side rejects. They fire
    // for every 5m candle that straddles both OR bounds; the engine
    // correctly admits one side and rejects the other. Noise, not
    // signal. Same filter applied to the per-pid Val/Gene feed.
    events = events.filter(function (ev) {
      if ((ev.kind || "").toLowerCase() !== "reject") return true;
      return String(ev.detail || "").indexOf("opposite_side:") === -1;
    });
    var body = document.getElementById("v10-act-body");
    var countEl = document.getElementById("v10-act-count");
    var summaryEl = document.getElementById("v10-act-summary");
    if (countEl) countEl.textContent = "· " + events.length;
    if (summaryEl) {
      if (events.length === 0) {
        summaryEl.textContent = "no events yet";
      } else {
        var first = events[0];
        // v8.3.1 -- convert UTC ISO to ET via the shared helper so the
        // operator sees the market clock instead of the storage clock.
        var hhmm = (typeof utcIsoToLocalHHMM === "function")
          ? utcIsoToLocalHHMM(first.ts_iso || "")
          : (first.ts_iso || "");
        summaryEl.textContent = "most recent · " + hhmm;
      }
    }
    if (!body) return;
    if (events.length === 0) {
      // v9.1.32 -- off-hours: collapse to a single dim line instead
      // of showing the full "waiting" card with an empty body.
      var _actMode = ((s.regime || {}).mode || "CLOSED");
      var _actRth = (_actMode === "OPEN" || _actMode === "OR"
        || _actMode === "POWER" || _actMode === "PRE");
      var _actSession = !!((s.v10 || {}).session_date);
      if (!_actRth && !_actSession) {
        body.innerHTML = '<div class="empty" style="font-size:11px;padding:10px 14px">'
          + '&mdash; session starts 09:25 ET &mdash;</div>';
      } else {
        body.innerHTML = '<div class="empty">No v10 events yet today.</div>';
      }
      return;
    }
    var rows = [];
    for (var i = 0; i < events.length; i++) {
      var e = events[i];
      // v8.3.1 -- per-row time in ET (was raw HHMM from UTC ISO).
      var hhmm2 = (typeof utcIsoToLocalHHMM === "function")
        ? utcIsoToLocalHHMM(e.ts_iso || "")
        : (e.ts_iso || "—");
      if (!hhmm2) hhmm2 = "—";
      var kindCls = "act-kind-" + (e.kind || "info");
      var kindTxt = (e.kind || "info").toUpperCase().replace(/_/g, " ");
      var ticker = e.ticker || "—";
      var pid = e.pid ? '<span class="act-pid">' + esc(e.pid) + '</span>' : '';
      rows.push(
        '<div class="act-row">' +
          '<span class="act-time">' + esc(hhmm2) + '</span>' +
          '<span class="act-ticker">' + esc(ticker) + '</span>' +
          '<span class="act-kind ' + kindCls + '">' + esc(kindTxt) + '</span>' +
          pid +
          '<span class="act-detail">' + esc(e.detail || "") + '</span>' +
        '</div>'
      );
    }
    body.innerHTML = '<div id="v10-act-list">' + rows.join("") + '</div>';
  }

  function renderV10Projection(p) {
    if (!p) return;
    var setText = function (id, t) {
      var el = document.getElementById(id);
      if (el) el.textContent = t;
    };
    setText("v10-proj-cagr", fmtPct(p.in_sample_cagr_pct, 1));
    setText(
      "v10-proj-range",
      fmtPct(p.honest_cagr_low_pct, 1) + " to " + fmtPct(p.honest_cagr_high_pct, 1)
    );
    setText("v10-proj-sharpe", p.sharpe_ann == null ? "—" : p.sharpe_ann.toFixed(2));
    setText("v10-proj-mdd", fmtPct(p.max_drawdown_pct, 2));
    setText("v10-proj-wr", p.win_rate_pct == null ? "—" : p.win_rate_pct.toFixed(1) + "%");
    // v7.64.0 -- "Live $0 / Δ -100%" bug fix. /api/v10/projection
    // backend calls PortfolioBook.current_equity() which returns 0 if
    // the new portfolio_book registry isn't initialized (a known v7.x
    // edge case during boot or in certain harness configs). The
    // headline Equity KPI uses tg._ssm().paper_cash + MTM via
    // _equity() and is always correct; mirror that source here.
    var liveBal = null;
    var startBal = (typeof p.starting_balance === "number") ? p.starting_balance : null;
    try {
      var s = window.__tgLastState;
      var eq = s && s.portfolio && s.portfolio.equity;
      if (typeof eq === "number" && eq > 0) liveBal = eq;
      var startK = s && s.portfolio && s.portfolio.start;
      if (typeof startK === "number" && startK > 0) startBal = startK;
    } catch (e) { /* fall through to payload value */ }
    if (liveBal == null && typeof p.live_balance === "number" && p.live_balance > 0) {
      liveBal = p.live_balance;
    }
    setText("v10-proj-live-balance", liveBal == null ? "—" : fmtMoney(liveBal));
    var growth = null;
    if (liveBal != null && startBal != null && startBal > 0) {
      growth = 100.0 * (liveBal - startBal) / startBal;
    } else if (typeof p.live_growth_pct === "number") {
      growth = p.live_growth_pct;
    }
    setText("v10-proj-growth", growth == null ? "—" : fmtPct(growth, 2));
    var growthEl = document.getElementById("v10-proj-growth");
    if (growthEl) {
      growthEl.style.color = growth == null
        ? "#9ca3af"
        : (growth >= 0 ? "#22c55e" : "#dc2626");
    }
  }

  // 60-second poll for /api/v10/projection. The static keystone numbers
  // don't change; live balance + growth are now sourced from
  // window.__tgLastState in renderV10Projection (v7.64.0) so they
  // refresh on every state tick, not just every 60s.
  // v7.64.0 -- cache the last payload + expose a refresh hook for the
  // applyState path to call with the cached payload so the live pair
  // tracks the Equity KPI in real time.
  var _v10ProjLastPayload = null;
  if (typeof window !== "undefined") {
    window.__tgRefreshV10Baseline = function () {
      if (_v10ProjLastPayload) {
        try { renderV10Projection(_v10ProjLastPayload); } catch (e) {}
      }
    };
  }
  (function initV10ProjectionPoll() {
    var run = function () {
      fetch("/api/v10/projection", { credentials: "same-origin" })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (p) {
          if (p) _v10ProjLastPayload = p;
          try { renderV10Projection(p); } catch (e) {}
        })
        .catch(function () { /* silent */ });
    };
    run();
    setInterval(run, 60000);
  })();

})();
