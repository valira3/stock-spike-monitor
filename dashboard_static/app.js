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
  function fmtPct(v, digits) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    const abs = Math.abs(v);
    const d = digits ?? (abs < 0.1 ? 3 : 2);
    return (v >= 0 ? "+" : "−") + abs.toFixed(d) + "%";
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
      const hh = String(d.getHours()).padStart(2, "0");
      const mm = String(d.getMinutes()).padStart(2, "0");
      const ss = String(d.getSeconds()).padStart(2, "0");
      return `${hh}:${mm}:${ss}`;
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
    $("k-pnl-sub").innerHTML = `${tradesLen} trade${tradesLen===1?"":"s"} · <span class="${pctCls}">${fmtPct(pct)}</span>`;

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
    $("pos-count").textContent = `· ${positions.length}`;
    const body = $("pos-body");
    const strip = $("port-strip");
    const emptyStrip = $("port-strip-empty");
    // v4.10.1 — also toggle the .is-empty modifier on the card itself so
    // the grid-2 stretch + flex-column min-heights collapse cleanly. The
    // CSS rule defeats grid stretch (align-self:start) so the card sizes
    // to header + one-row strip instead of matching the Proximity card.
    const card = body && body.parentElement;

    if (positions.length === 0) {
      // v4.10.0 — collapsed empty state. Hide the "No open positions."
      // body + the 2-row Equity/BP/Cash/Invested/Shorted strip, and
      // show a single-row condensed strip with just the three values
      // an operator actually wants at a glance during off-hours.
      body.innerHTML = "";
      body.style.display = "none";
      strip.style.display = "none";
      if (card) card.classList.add("is-empty");
      const p = sl.portfolio || {};
      if (emptyStrip) {
        if (typeof p.equity === "number") {
          const bp = (typeof p.cash === "number" && typeof p.short_liab === "number")
            ? (p.cash - p.short_liab) : null;
          $("pse-equity").textContent = fmtUsd(p.equity);
          $("pse-bp").textContent = bp === null ? "—" : fmtUsd(bp);
          $("pse-cash").textContent = fmtUsd(p.cash);
          emptyStrip.style.display = "grid";
        } else {
          emptyStrip.style.display = "none";
        }
      }
      return;
    } else {
      body.style.display = "";
      if (card) card.classList.remove("is-empty");
      if (emptyStrip) emptyStrip.style.display = "none";
      const rows = positions.map((p) => {
        const sideCls = p.side === "SHORT" ? "side-short" : "side-long";
        const markCls = p.side === "SHORT" ? "mark-short" : "mark-long";
        const pnlCls = (p.unrealized ?? 0) >= 0 ? "delta-up" : "delta-down";
        const eff = (typeof p.effective_stop === "number")
                      ? p.effective_stop : p.stop;
        // v6.0.6 \u2014 TRAIL badge fires for either trail mechanism:
        //   * legacy trail_active (Phase B/C breakeven trail, sets
        //     pos.trail_stop and pos.trail_active=True)
        //   * Alarm-F chandelier (mutates pos.stop directly via
        //     Sentinel; surfaces as p.chandelier_stage >= 1).
        // Without this, an actively ratcheting chandelier looks like
        // a static hard stop on the dashboard even though the engine
        // is tightening it every minute.
        const _trailArmed = p.trail_active
          || (Number.isFinite(Number(p.chandelier_stage)) && Number(p.chandelier_stage) >= 1);
        const trailBadge = _trailArmed
          ? ` <span class="trail-badge" title="Trail stop is armed \u2014 the effective stop now follows price, not the original hard stop">TRAIL</span>`
          : "";
        // v5.13.10 — SB (Alarm A1 Loss distance) column removed
        // per operator request. Phase badge stays: A = fresh entry,
        // B = first runner / partial taken, C = mature ratcheting trail.
        const phase = (p.phase === "B" || p.phase === "C") ? p.phase : "A";
        const phaseTitle = (phase === "A")
          ? "Phase A \u2014 fresh entry, hard stop only"
          : (phase === "B")
            ? "Phase B \u2014 first runner / partial taken, breakeven trail"
            : "Phase C \u2014 mature runner, ratcheting trail stop";
        const phaseBadge = `<span class="eot-phase-badge eot-phase-${phase}" title="${escapeHtml(phaseTitle)}">${phase}</span>`;
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
        return `<tr data-pos-ticker="${escapeHtml(p.ticker)}" tabindex="0" role="button" aria-controls="pmtx-body" style="cursor:pointer">
          <td><span class="ticker">${escapeHtml(p.ticker)} <span class="mark ${markCls}" title="${escapeHtml(dotTitle)}">●</span></span>${phaseBadge}</td>
          <td><span class="${sideCls}">${p.side}</span></td>
          <td class="right">${p.shares}</td>
          <td class="right">${fmtPx(p.entry)}</td>
          <td class="right">${fmtPx(p.mark)}</td>
          <td class="right">${fmtPx(eff)}${trailBadge}</td>
          <td class="right ${pnlCls}">${fmtUsd(p.unrealized)}</td>
          <td class="right ${pnlCls}">${pctTxt}</td>
        </tr>`;
      }).join("");
      body.innerHTML = `<table>
        <thead><tr>
          <th title="Symbol \u00b7 colored dot shows side (green = long, red = short)">Ticker</th>
          <th title="LONG = bought to open. SHORT = sold to open.">Side</th>
          <th class="right" title="Number of shares">Sh</th>
          <th class="right" title="Average fill price when the position opened">Entry</th>
          <th class="right" title="Latest mark price">Mark</th>
          <th class="right" title="Effective stop \u2014 trail stop if armed (TRAIL badge), otherwise the hard stop">Stop</th>
          <th class="right" title="Unrealized profit/loss in dollars at the current mark">Unreal.</th>
          <th class="right" title="Unrealized P&L as a percent of cost basis (entry x shares)">%</th>
        </tr></thead>
        <tbody>${rows}</tbody></table>`;
    }

    // v5.21.0 — Click-to-Titan: clicking any position row expands the
    // matching Titan in the Permit Matrix and scrolls it into view.
    // Wire once using __posClickWired sentinel (renderPositions rebuilds
    // innerHTML on every SSE tick, so we must not re-attach every time).
    if (!body.__posClickWired) {
      body.addEventListener("click", function _posRowClick(ev) {
        const tr = ev.target.closest("tr[data-pos-ticker]");
        if (!tr) return;
        const ticker = tr.getAttribute("data-pos-ticker");
        if (!ticker) return;
        // v5.23.0 — Locate the Permit Matrix body in the *active* tab.
        // The Main tab body uses id="pmtx-body" while Val/Gene panels
        // use data-f="pmtx-body". The previous selector only matched
        // Val/Gene, so clicking from the Main positions table either
        // hit the wrong (hidden) panel or no-op'd entirely. Try the
        // visible candidates in order: Main id, then any panel whose
        // own data-f body is currently in the viewport flow.
        let pmtxBody = document.getElementById("pmtx-body");
        if (!pmtxBody) {
          // Fallback: pick the data-f body inside the currently active
          // tab panel (data-tg-active-tab on body).
          const activeTab = document.body.getAttribute("data-tg-active-tab") || "main";
          const activePanel = document.getElementById("tg-panel-" + activeTab);
          if (activePanel) {
            pmtxBody = activePanel.querySelector('[data-f="pmtx-body"]');
          }
        }
        if (!pmtxBody) {
          // Last-resort: any data-f pmtx-body in the document.
          pmtxBody = document.querySelector('[data-f="pmtx-body"]');
        }
        if (!pmtxBody) return;
        const titanRow = pmtxBody.querySelector('tr.pmtx-row[data-pmtx-tkr="' + ticker + '"]');
        if (!titanRow) return; // position exists but no Titan row (stale/delisted)
        // Single-open semantics: clear existing expansion, add this ticker.
        if (!pmtxBody.__pmtxExpandedSet) pmtxBody.__pmtxExpandedSet = new Set();
        pmtxBody.__pmtxExpandedSet.clear();
        pmtxBody.__pmtxExpandedSet.add(ticker);
        if (typeof pmtxBody.__pmtxApplyExpanded === "function") pmtxBody.__pmtxApplyExpanded();
        // Update aria-expanded on the clicked row.
        const allPosTrs = body.querySelectorAll("tr[data-pos-ticker]");
        allPosTrs.forEach((r) => r.setAttribute("aria-expanded", "false"));
        tr.setAttribute("aria-expanded", "true");
        // Scroll the Titan row into view only when it is in the DOM.
        if (document.body.contains(titanRow)) {
          titanRow.scrollIntoView({ behavior: "smooth", block: "center" });
        }
      });
      // Keyboard: Enter/Space on a focused position row triggers expand.
      body.addEventListener("keydown", function _posRowKey(ev) {
        if (ev.key !== "Enter" && ev.key !== " ") return;
        const tr = ev.target.closest("tr[data-pos-ticker]");
        if (!tr) return;
        ev.preventDefault();
        tr.click();
      });
      body.__posClickWired = true;
    }

    // portfolio strip always shows if portfolio data present
    const p = sl.portfolio;
    if (p && typeof p.equity === "number") {
      strip.style.display = "block";
      $("port-cash").textContent = fmtUsd(p.cash);
      $("port-longmv").textContent = fmtUsd(p.long_mv);
      $("port-shortliab").textContent = fmtUsd(p.short_liab);
      $("port-equity").textContent = fmtUsd(p.equity);
      // Buying power = cash unencumbered by short-sale liability.
      // Short-sale proceeds sit in cash but are owed back, so
      // (cash − short_liab) is the amount that's actually spendable
      // without widening short exposure.
      const bp = (typeof p.cash === "number" && typeof p.shortLiab === "number")
        ? (p.cash - p.shortLiab)
        : (typeof p.cash === "number" && typeof p.short_liab === "number"
            ? (p.cash - p.short_liab)
            : null);
      $("port-bp").textContent = (bp === null) ? "—" : fmtUsd(bp);
    } else {
      strip.style.display = "none";
    }
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
      const isOpen  = (act === "BUY" || act === "SHORT");
      const isClose = (act === "SELL" || act === "COVER");
      const side  = t.side || "LONG";
      const sym   = t.ticker || "—";
      const shares = t.shares;
      const px    = t.price ?? t.entry_price ?? t.exit_price;

      // Action chip — open (green) / close (red). Symbol still
      // carries LONG/SHORT colour coding to avoid double-cueing.
      const actCls = isClose ? "act-sell" : "act-buy";
      const actLbl = act || (side === "SHORT" ? "SHORT" : "LONG");

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
        +     '<span class="pmtx-comp-chip">' + escapeHtml(chip) + '</span>'
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
  function renderWeatherCheck(s, panel) {
    const root = _pmtxEl(panel, "pmtx-weather");
    const icon = _pmtxEl(panel, "pmtx-weather-icon");
    const verdict = _pmtxEl(panel, "pmtx-weather-verdict");
    const detail = _pmtxEl(panel, "pmtx-weather-detail");
    const stats = _pmtxEl(panel, "pmtx-weather-stats");
    if (!root || !icon || !verdict || !detail) return;
    const ts = (s && s.tiger_sovereign) || null;
    const p1 = (ts && ts.phase1) || {};
    const longBlk = p1.long || {};
    const shortBlk = p1.short || {};
    const longPermit = !!longBlk.permit;
    const shortPermit = !!shortBlk.permit;
    const haveData = (typeof longBlk.qqq_5m_close === "number")
                  || (typeof shortBlk.qqq_5m_close === "number");

    root.classList.remove(
      "pmtx-weather-pending", "pmtx-weather-green",
      "pmtx-weather-red",     "pmtx-weather-amber"
    );

    if (!ts || !haveData) {
      root.classList.add("pmtx-weather-pending");
      icon.textContent = "\u00B7";
      verdict.textContent = "Waiting for permit state\u2026";
      detail.textContent = "QQQ 5m close vs 9-EMA \u00B7 QQQ vs AVWAP_0930";
      if (stats) stats.innerHTML = "";
      return;
    }

    let cls = "pmtx-weather-red";
    let icoTxt = "\u2715";
    let verdictTxt = "NO permit \u00B7 stand down";
    let detailTxt = "QQQ has not cleared either Phase 1 gate \u2014 entries blocked on both sides.";
    if (longPermit && shortPermit) {
      cls = "pmtx-weather-amber";
      icoTxt = "!";
      verdictTxt = "BOTH-side permit \u00B7 chop risk";
      detailTxt = "QQQ has crossed both sides recently \u2014 prefer the higher-conviction setup.";
    } else if (longPermit) {
      cls = "pmtx-weather-green";
      icoTxt = "\u2713";
      verdictTxt = "PERMIT \u00B7 LONG side open";
      detailTxt = "QQQ 5m close > 9-EMA and last > AVWAP_0930 \u2014 long entries enabled.";
    } else if (shortPermit) {
      cls = "pmtx-weather-green";
      icoTxt = "\u2713";
      verdictTxt = "PERMIT \u00B7 SHORT side open";
      detailTxt = "QQQ 5m close < 9-EMA and last < AVWAP_0930 \u2014 short entries enabled.";
    }
    root.classList.add(cls);
    icon.textContent = icoTxt;
    verdict.textContent = verdictTxt;
    detail.textContent = detailTxt;

    // Numeric stats column — prefer the side that has data.
    const ref = (typeof longBlk.qqq_last === "number") ? longBlk : shortBlk;
    const qqqLast = (typeof ref.qqq_last === "number") ? ref.qqq_last : null;
    const ema9    = (typeof ref.qqq_5m_ema9 === "number") ? ref.qqq_5m_ema9 : null;
    const avwap   = (typeof ref.qqq_avwap_0930 === "number") ? ref.qqq_avwap_0930 : null;
    if (stats) {
      stats.innerHTML = ''
        + '<div class="pmtx-weather-stat" title="QQQ last trade price"><span class="pmtx-weather-stat-num">' + escapeHtml(_pmtxNum(qqqLast)) + '</span><span>QQQ last</span></div>'
        + '<div class="pmtx-weather-stat" title="QQQ 5-minute 9-EMA \u2014 long permit needs close > EMA9"><span class="pmtx-weather-stat-num">' + escapeHtml(_pmtxNum(ema9)) + '</span><span>EMA9 (5m)</span></div>'
        + '<div class="pmtx-weather-stat" title="QQQ Anchored VWAP from 09:30 ET \u2014 long permit needs last > AVWAP"><span class="pmtx-weather-stat-num">' + escapeHtml(_pmtxNum(avwap)) + '</span><span>AVWAP 09:30</span></div>';
    }
  }

  // \u2500\u2500\u2500 Permit Matrix (per-Titan row table) \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
  // v5.18.1 \u2014 optional `panel` arg lets Val/Gene tabs reuse the
  // exact same renderer (data is market-wide, just different DOM mount).
  function renderPermitMatrix(s, panel) {
    const body = _pmtxEl(panel, "pmtx-body");
    const countEl = _pmtxEl(panel, "pmtx-count");
    const chip = _pmtxEl(panel, "pmtx-overall-chip");
    if (!body) return;

    const ts = (s && s.tiger_sovereign) || null;
    const tickers = (s && Array.isArray(s.tickers)) ? s.tickers.slice() : [];
    if (!ts || tickers.length === 0) {
      if (countEl) countEl.textContent = "\u00B7 \u2014";
      if (chip) chip.textContent = "\u2014";
      body.innerHTML = '<div class="empty">Waiting for matrix data\u2026</div>';
      return;
    }
    if (countEl) countEl.textContent = "\u00B7 " + tickers.length;

    const p1 = ts.phase1 || {};
    const longPermit  = !!(p1.long  && p1.long.permit);
    const shortPermit = !!(p1.short && p1.short.permit);
    if (chip) {
      let label = "NO permit";
      if (longPermit && shortPermit) label = "BOTH-side";
      else if (longPermit) label = "LONG";
      else if (shortPermit) label = "SHORT";
      chip.textContent = label;
    }

    const idx = _pmtxIndex(ts);
    const positions = (s && Array.isArray(s.positions)) ? s.positions : [];
    const positionsByTicker = {};
    positions.forEach((p) => { if (p && p.ticker) positionsByTicker[p.ticker] = p; });
    const trades = (s && Array.isArray(s.trades_today)) ? s.trades_today : [];
    const tradesByTicker = {};
    trades.forEach((t) => {
      if (!t || !t.ticker) return;
      if (!tradesByTicker[t.ticker]) tradesByTicker[t.ticker] = [];
      tradesByTicker[t.ticker].push(t);
    });
    // v5.18.0 — fold standalone Proximity card into the matrix. Each
    // /api/state.proximity row carries price + nearest_pct + nearest_label
    // for one ticker; index by ticker so _pmtxBuildRow can look it up.
    const proximity = (s && Array.isArray(s.proximity)) ? s.proximity : [];
    const proximityByTicker = {};
    proximity.forEach((r) => { if (r && r.ticker) proximityByTicker[r.ticker] = r; });

    // v5.20.5 \u2014 expanded card metrics. /api/state now ships per-ticker
    // and per-position v510 metric blocks (di / vol_bucket / boundary_hold
    // / sovereign_brake / velocity_fuse / strikes); the regime block is
    // already shipped by phase1. Pass the lookups down to _pmtxBuildRow
    // so each card can render numeric rows beneath its state badge.
    const perTickerV510 = (s && s.per_ticker_v510) || {};
    const perPositionV510 = (s && s.per_position_v510) || {};
    const regimeBlock = (s && s.regime) || {};
    const sectionIPermit = (s && s.section_i_permit) || null;

    // v5.29.0 — hide bypassed components driven by /api/state.feature_flags.
    // Volume column / card hide when volume_gate_enabled=false; sentinel-strip
    // cells for Alarms C/D/E hide when their respective alarm_*_enabled flags
    // are false. Defaults are conservative (hide) so a missing flag block
    // matches production behaviour. Flags read once per render and threaded
    // down to _pmtxBuildRow so every helper sees a consistent view.
    const ff = (s && s.feature_flags) || {};
    const showVolume = !!ff.volume_gate_enabled;
    const showAlarmC = !!ff.alarm_c_enabled;
    const showAlarmD = !!ff.alarm_d_enabled;
    const showAlarmE = !!ff.alarm_e_enabled;
    // v5.30.0 — alarm_f_enabled defaults to true (Alarm F has no kill
    // switch). Older /api/state payloads without the key will keep F
    // visible, matching legacy behaviour.
    const showAlarmF = (ff.alarm_f_enabled !== false);

    // v6.1.1 \u2014 surface v6.1.0 strategy feature flags so existing
    // expanded-row cards (Phase 2 Boundary, Phase 3 Authority, Alarm F)
    // can decorate their value text with the active strategy state.
    // Defaults to a conservative all-off shape so a missing v610_flags
    // block (older deploys) renders as legacy.
    const v610Flags = (s && s.v610_flags) || {};
    // v6.2.0 \u2014 entry-loosening feature flags. Same shape pattern as
    // v610_flags; missing block degrades to legacy text on the Local
    // Weather, Boundary, and Momentum cards.
    const v620Flags = (s && s.v620_flags) || {};
    // v6.3.0 \u2014 Sentinel B noise-cross filter flags. Surfaced as a
    // suffix on the B Trend Death sentinel cell and the Local Weather
    // card so operators can see the active noise threshold at a glance.
    const v630Flags = (s && s.v630_flags) || {};
    // v6.4.0 \u2014 Alarm B disable + Chandelier multiplier tightening flags.
    // Same surface pattern as the v6.3.0 block. Used by the B Trend Death
    // sentinel cell (renders DISABLED state when alarm_b_enabled=false) and
    // by the Local Weather card suffix (chand 1.5/0.7 instead of
    // noise\u2265k\u00d7ATR when B is off).
    const v640Flags = (s && s.v640_flags) || {};

    const rowsHtml = [];
    tickers.forEach((tkr) => {
      const built = _pmtxBuildRow(
        tkr, idx, positionsByTicker, tradesByTicker, proximityByTicker,
        longPermit, shortPermit,
        perTickerV510, perPositionV510, regimeBlock, sectionIPermit,
        { showVolume: showVolume, showAlarmC: showAlarmC, showAlarmD: showAlarmD, showAlarmE: showAlarmE, showAlarmF: showAlarmF, v610Flags: v610Flags, v620Flags: v620Flags, v630Flags: v630Flags, v640Flags: v640Flags }
      );
      rowsHtml.push(built.tableRows);
    });

    // v5.18.0 — row layout collapsed to match the original mockup:
    // one ~38px row per Titan; the per-position sentinel strip is no
    // longer always rendered — it's an expand-on-click detail row.
    // "DI+ 1m" column was dropped (always pending in v5.16/v5.18 —
    // not surfaced by /api/state). "Last trade" moved into the detail
    // panel; "Price · Distance" replaces it as the trailing cell so
    // the standalone Proximity card retires cleanly.
    // v5.19.2 \u2014 mobile shows ALL columns (ADX / DI\u00b1 / Vol) like
    // desktop; the table-wrap allows horizontal scroll if the row
    // exceeds the viewport. Headers are condensed (Vol, Dist, DI\u00b1
    // 5m>25) and OR-high/OR-low render as ORH/ORL in the body.
    body.innerHTML = ''
      + '<div class="pmtx-table-wrap">'
      +   '<table class="pmtx-table' + (showVolume ? '' : ' pmtx-no-volume') + '"><thead><tr>'
      +     '<th class="pmtx-col-titan">Titan</th>'
      +     '<th class="pmtx-col-weather" title="v5.31.5 per-stock local weather. Glyph: x = no permit (global QQQ blocks both sides AND no local override); green up arrow = long-aligned local weather; green down arrow = short-aligned local weather. Local weather is (5m close past EMA9 OR last past opening AVWAP) AND 1m DI confirmation, evaluated per ticker.">Weather</th>'
      +     '<th class="pmtx-col-orb" title="Phase 2 Boundary card. Strike 1: two consecutive 1m closes strictly above ORH (long) or below ORL (short), with ORH/ORL frozen at exactly 09:35:59 ET. Strikes 2 & 3: two consecutive 1m closes above the running NHOD (long) or below the running NLOD (short).">Boundary</th>'
      +     (showVolume ? '<th class="pmtx-col-vol" title="Phase 2 Volume card. 1m volume must be \u2265 100% of the 55-bar rolling average. REQUIRED after 10:00 AM ET; before 10:00 ET the gate auto-passes. Bypassed when VOLUME_GATE_ENABLED=false.">Volume</th>' : '')
      +     '<th class="pmtx-col-diplus" title="Phase 3 Authority card. Section-I permit alignment: cell goes green when at least one of long_open / short_open is true on section_i_permit. Per-ticker DI\u00b1 detail (DI+ 1m/5m, DI\u2212 1m/5m, threshold) lives in the Momentum card metric stack inside the expanded row.">Authority</th>'
      +     '<th class="pmtx-col-adx" title="Phase 3 Momentum card. Required for entry: 5m ADX > 20 AND Alarm E = FALSE. This is a primary spec gate \u2014 if ADX \u2264 20 the bot does not open a Strike, regardless of DI\u00b1.">Momentum</th>'
      +     '<th class="pmtx-col-strike" title="Strike sequence (v15.0 \u00a71). Maximum 3 Strikes per ticker per day. Sequential Requirement: a subsequent strike cannot initiate until the previous position is fully flat (Position = 0). Counters reset at 09:30:00 ET.">Strikes</th>'
      +     '<th class="pmtx-col-state" title="Per-ticker FSM \u2014 IDLE \u00b7 ARMED (Phase 1 weather + Phase 2 permit satisfied, awaiting Phase 3 authority + momentum) \u00b7 IN POS \u00b7 LOCKED (3-of-3 strikes used).">State</th>'
      +     '<th class="pmtx-col-prox" title="Live last price \u00b7 distance to the live boundary the next strike is hunting. Strike 1 hunts ORH/ORL (frozen 09:35:59); strikes 2 & 3 hunt the running NHOD/NLOD.">Dist</th>'
      +     '<th class="pmtx-col-mini" title="v6.0.0 mini-chart. Today\u2019s 1m closes downsampled to fit a 60-pt sparkline. Green tint when last > open, red when last < open. Hover for hi/lo/last.">Today</th>'
      +     '<th class="pmtx-col-expand" aria-label="Toggle detail"></th>'
      +   '</tr></thead><tbody>' + rowsHtml.join("") + '</tbody></table>'
      + '</div>';

    // Wire up the click-to-expand toggle. One delegated handler attached
    // to body so we don't leak listeners on every re-render.
    //
    // v5.19.4 \u2014 sticky expand. Previously the expanded class was set
    // on the live DOM only, so the next /api/state push (every 1\u20132s
    // via SSE) re-built body.innerHTML and wiped the class \u2014 the
    // user saw the row "immediately collapse". The fix:
    //   1. Track expanded ticker(s) in body.__pmtxExpandedSet (Set).
    //   2. After every render (just below) re-apply the classes.
    //   3. Click handler updates the Set, then re-applies. Single-open
    //      semantics: clicking a different row replaces the prior
    //      expansion. Re-clicking the same row collapses it.
    //   4. Document-level "click outside" listener clears the Set so
    //      clicking anywhere outside the matrix collapses the open row.
    if (!body.__pmtxExpandedSet) body.__pmtxExpandedSet = new Set();
    function _pmtxApplyExpanded() {
      const set = body.__pmtxExpandedSet || new Set();
      const allMain = body.querySelectorAll("tr.pmtx-row[data-pmtx-tkr]");
      allMain.forEach((r) => {
        const t = r.getAttribute("data-pmtx-tkr");
        const want = set.has(t);
        r.classList.toggle("pmtx-row-expanded", want);
      });
      const allDetail = body.querySelectorAll("tr.pmtx-detail-row[data-pmtx-tkr]");
      allDetail.forEach((r) => {
        const t = r.getAttribute("data-pmtx-tkr");
        r.classList.toggle("pmtx-detail-open", set.has(t));
      });
      // v5.23.0 — hydrate the intraday chart inside each open detail
      // row. _pmtxHydrateIntradayCharts() is idempotent (TTL-cached) so
      // re-calling on every apply is cheap; skipping the call when no
      // rows are open avoids any DOM scan when the matrix is collapsed.
      if (set.size > 0 && typeof _pmtxHydrateIntradayCharts === "function") {
        try { _pmtxHydrateIntradayCharts(body); } catch (e) {}
      }
    }
    body.__pmtxApplyExpanded = _pmtxApplyExpanded;
    if (!body.__pmtxExpandWired) {
      // v6.0.1 \u2014 when a row collapses (re-click toggle or outside-
      // click), drop its persisted chart view so the next time it opens
      // we start at the full session window. Mutates _chartViewByTkr in
      // place; safe to call with an empty/undefined set.
      function _pmtxResetChartViewsFor(set) {
        if (!set || typeof set.forEach !== "function") return;
        set.forEach(function (t) {
          if (t && _chartViewByTkr && _chartViewByTkr[t]) {
            delete _chartViewByTkr[t];
          }
        });
      }
      body.addEventListener("click", function (ev) {
        const trigger = ev.target.closest("tr.pmtx-row[data-pmtx-tkr]");
        if (!trigger) return;
        const tkr = trigger.getAttribute("data-pmtx-tkr");
        const detail = body.querySelector('tr.pmtx-detail-row[data-pmtx-tkr="' + tkr + '"]');
        if (!detail) return; // no detail to expand for this row
        const set = body.__pmtxExpandedSet;
        const wasOpen = set.has(tkr);
        // Reset persisted chart views for every ticker leaving the open
        // set. Single-open semantics: if a different row was open, that
        // ticker collapses too. If we're re-clicking the same row, that
        // ticker also leaves the set.
        _pmtxResetChartViewsFor(set);
        set.clear(); // single-open semantics
        if (!wasOpen) set.add(tkr);
        _pmtxApplyExpanded();
      });
      // Outside-click collapses any open detail. Capture phase so the
      // matrix's own click handler still wins for in-matrix clicks.
      document.addEventListener("click", function (ev) {
        if (!body.__pmtxExpandedSet || body.__pmtxExpandedSet.size === 0) return;
        // If the click landed on a tab button, header chip, or anything
        // outside this body, collapse all expanded rows.
        if (!body.contains(ev.target)) {
          _pmtxResetChartViewsFor(body.__pmtxExpandedSet);
          body.__pmtxExpandedSet.clear();
          if (typeof body.__pmtxApplyExpanded === "function") body.__pmtxApplyExpanded();
        }
      });
      body.__pmtxExpandWired = true;
    }
    // Re-apply after every render (innerHTML above wiped the classes).
    _pmtxApplyExpanded();
  }

  // v5.23.0 — Intraday chart panel placeholder + hydration. The
  // expanded Titan card needs a per-ticker chart of premarket + RTH
  // 1m bars (5m on mobile) with OR high/low, anchored VWAP, 5m EMA9,
  // and entry/exit markers. We emit a lightweight Canvas placeholder
  // here and hydrate it post-render so the matrix HTML build stays
  // O(N) and synchronous \u2014 fetching bars during render would
  // serialize requests against the same /api/state interval. Returns
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
      +     '<span class="pmtx-intraday-leg pmtx-intraday-leg-avwap">AVWAP \u00b11\u03c3</span>'
      +     '<span class="pmtx-intraday-leg pmtx-intraday-leg-ema9">EMA9 (5m)</span>'
      +     '<span class="pmtx-intraday-leg pmtx-intraday-leg-pdc">PDC</span>'
      +     '<span class="pmtx-intraday-leg pmtx-intraday-leg-hod">HOD/LOD</span>'
      +     '<span class="pmtx-intraday-leg pmtx-intraday-leg-vol">Volume</span>'
      +     '<span class="pmtx-intraday-leg pmtx-intraday-leg-sentinel">Sentinel</span>'
      +     '<span class="pmtx-intraday-leg pmtx-intraday-leg-entry">Entry</span>'
      +     '<span class="pmtx-intraday-leg pmtx-intraday-leg-exit">Exit</span>'
      +     '<span class="pmtx-intraday-leg pmtx-intraday-leg-trail">Trail stop</span>'
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
  const _CHART_FULL_X_MIN = 480;
  const _CHART_FULL_X_MAX = 1080;
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
        xMin: persisted ? persisted.xMin : _CHART_FULL_X_MIN,
        xMax: persisted ? persisted.xMax : _CHART_FULL_X_MAX,
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
    let X_MIN = (typeof _vs.xMin === "number") ? _vs.xMin : 240;
    let X_MAX = (typeof _vs.xMax === "number") ? _vs.xMax : 1200;
    if (X_MAX - X_MIN < 30) X_MAX = X_MIN + 30; // floor at 30 min
    if (X_MIN < 240) X_MIN = 240;
    if (X_MAX > 1200) X_MAX = 1200;
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

    // v5.31.0 \u2014 PDC (prior-day close) dashed purple, HOD/LOD solid thin.
    const pdc = (typeof payload.pdc === "number") ? payload.pdc : null;
    const sessHod = (typeof payload.sess_hod === "number") ? payload.sess_hod : null;
    const sessLod = (typeof payload.sess_lod === "number") ? payload.sess_lod : null;
    if (pdc !== null && pdc >= yMin && pdc <= yMax) {
      ctx.strokeStyle = "#a78bfa";
      ctx.lineWidth = 1.2;
      ctx.setLineDash([5, 4]);
      ctx.beginPath();
      ctx.moveTo(PAD_L, yOf(pdc)); ctx.lineTo(PAD_L + plotW, yOf(pdc));
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = "#a78bfa";
      ctx.font = "10px system-ui, sans-serif";
      ctx.textAlign = "left";
      ctx.fillText("PDC", PAD_L + 4, yOf(pdc) - 2);
    }
    if (sessHod !== null && sessHod >= yMin && sessHod <= yMax) {
      ctx.strokeStyle = "#34d399";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(PAD_L, yOf(sessHod)); ctx.lineTo(PAD_L + plotW, yOf(sessHod));
      ctx.stroke();
      ctx.fillStyle = "#34d399";
      ctx.font = "10px system-ui, sans-serif";
      ctx.textAlign = "left";
      ctx.fillText("HOD", PAD_L + 4, yOf(sessHod) - 2);
    }
    if (sessLod !== null && sessLod >= yMin && sessLod <= yMax) {
      ctx.strokeStyle = "#f87171";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(PAD_L, yOf(sessLod)); ctx.lineTo(PAD_L + plotW, yOf(sessLod));
      ctx.stroke();
      ctx.fillStyle = "#f87171";
      ctx.font = "10px system-ui, sans-serif";
      ctx.textAlign = "left";
      ctx.fillText("LOD", PAD_L + 4, yOf(sessLod) + 10);
    }

    // v5.31.0 \u2014 AVWAP \u00b11\u03c3 band, filled translucent under the AVWAP line.
    // Band points come from payload.bars[].avwap_hi / .avwap_lo (RTH-only,
    // null in premarket). We build a top polyline forward then bottom backward.
    {
      const top = [];
      const bot = [];
      for (const b of bars) {
        if (typeof b.et_min !== "number") continue;
        const hi = b.avwap_hi, lo = b.avwap_lo;
        if (hi === null || hi === undefined || lo === null || lo === undefined) {
          if (top.length) {
            ctx.fillStyle = "rgba(122,166,255,0.08)";
            ctx.beginPath();
            ctx.moveTo(top[0].x, top[0].y);
            for (let i = 1; i < top.length; i++) ctx.lineTo(top[i].x, top[i].y);
            for (let i = bot.length - 1; i >= 0; i--) ctx.lineTo(bot[i].x, bot[i].y);
            ctx.closePath();
            ctx.fill();
            top.length = 0; bot.length = 0;
          }
          continue;
        }
        const x = xOf(b.et_min);
        top.push({x: x, y: yOf(hi)});
        bot.push({x: x, y: yOf(lo)});
      }
      if (top.length) {
        ctx.fillStyle = "rgba(122,166,255,0.08)";
        ctx.beginPath();
        ctx.moveTo(top[0].x, top[0].y);
        for (let i = 1; i < top.length; i++) ctx.lineTo(top[i].x, top[i].y);
        for (let i = bot.length - 1; i >= 0; i--) ctx.lineTo(bot[i].x, bot[i].y);
        ctx.closePath();
        ctx.fill();
      }
    }

    // Candles (thin OHLC sticks). Body width scales with bar count.
    const bw = Math.max(1, Math.min(6, plotW / Math.max(bars.length, 1) - 1));
    for (const b of bars) {
      if (typeof b.et_min !== "number") continue;
      const x = xOf(b.et_min);
      const yH = yOf(b.h);
      const yL = yOf(b.l);
      const yO = yOf(b.o);
      const yC = yOf(b.c);
      const up = b.c >= b.o;
      ctx.strokeStyle = up ? "#3ec28f" : "#e26a6a";
      ctx.fillStyle = up ? "#3ec28f" : "#e26a6a";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x, yH); ctx.lineTo(x, yL);
      ctx.stroke();
      ctx.fillRect(x - bw / 2, Math.min(yO, yC), bw, Math.max(1, Math.abs(yC - yO)));
    }

    // v5.31.0 \u2014 Volume sub-pane histogram (slate bars, scaled to max v).
    {
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

    // AVWAP line. Pre-v5.31.0 reset on null bars (premarket); v5.31.0 also
    // draws PM AVWAP when bars carry it (lighter alpha) so the band+line is
    // continuous from 8am ET onward.
    ctx.strokeStyle = "#7aa6ff";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    let started = false;
    for (const b of bars) {
      if (typeof b.et_min !== "number") continue;
      if (b.avwap === null || b.avwap === undefined) { started = false; continue; }
      const x = xOf(b.et_min), y = yOf(b.avwap);
      if (!started) { ctx.moveTo(x, y); started = true; }
      else ctx.lineTo(x, y);
    }
    ctx.stroke();

    // v5.31.0 \u2014 Premarket AVWAP (anchored 8:00 ET, et_min<570) drawn at
    // 0.55 alpha so it visually fades into the RTH AVWAP after 9:30.
    {
      ctx.save();
      ctx.globalAlpha = 0.55;
      ctx.strokeStyle = "#7aa6ff";
      ctx.lineWidth = 1.2;
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      let pmStarted = false;
      for (const b of bars) {
        if (typeof b.et_min !== "number") continue;
        if (b.et_min >= 570) break;
        const v = b.pm_avwap;
        if (v === null || v === undefined) { pmStarted = false; continue; }
        const x = xOf(b.et_min), y = yOf(v);
        if (!pmStarted) { ctx.moveTo(x, y); pmStarted = true; }
        else ctx.lineTo(x, y);
      }
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.restore();
    }

    // EMA9 (5m) line. v5.31.0: relax the reset so PM bars draw too
    // (premarket EMA9 also produced when bars carry it).
    ctx.strokeStyle = "#c084fc";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    started = false;
    for (const b of bars) {
      if (typeof b.et_min !== "number") continue;
      if (b.ema9_5m === null || b.ema9_5m === undefined) { started = false; continue; }
      const x = xOf(b.et_min), y = yOf(b.ema9_5m);
      if (!started) { ctx.moveTo(x, y); started = true; }
      else ctx.lineTo(x, y);
    }
    ctx.stroke();

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

    // v5.31.0 \u2014 Sentinel arm/trip markers (diamonds). Amber = armed/changed,
    // red = fired. Source: payload.sentinel_events (ts_utc + price).
    const sentinelEvents = (payload && Array.isArray(payload.sentinel_events))
      ? payload.sentinel_events : [];
    for (const ev of sentinelEvents) {
      if (!ev || typeof ev.price !== "number") continue;
      const etMin = utcIsoToEtMin(ev.ts_utc);
      if (etMin === null) continue;
      if (etMin < X_MIN || etMin > X_MAX) continue;
      const x = xOf(etMin), y = yOf(ev.price);
      ctx.fillStyle = ev.fired ? "#ef4444" : "#fbbf24";
      ctx.beginPath();
      ctx.moveTo(x, y - 5);
      ctx.lineTo(x + 4, y);
      ctx.lineTo(x, y + 5);
      ctx.lineTo(x - 4, y);
      ctx.closePath();
      ctx.fill();
    }

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

    // Trail-stop staircase + stage transition ticks. Backend supplies each
    // point with ``et_min``, ``stop``, ``stage``.
    const trail = Array.isArray(lc.trail_series) ? lc.trail_series : [];
    if (trail.length) {
      ctx.strokeStyle = "#fbbf24";
      ctx.lineWidth = 1.2;
      ctx.setLineDash([4, 3]);
      ctx.beginPath();
      let prevX = null, prevY = null, prevStage = null;
      for (const pt of trail) {
        if (!pt) continue;
        const m = (typeof pt.et_min === "number") ? pt.et_min : null;
        if (!inWin(m)) continue;
        if (typeof pt.stop !== "number" || !inPrice(pt.stop)) continue;
        const x = xOf(m), y = yOf(pt.stop);
        if (prevX === null) {
          ctx.moveTo(x, y);
        } else {
          // Step: horizontal then vertical.
          ctx.lineTo(x, prevY);
          ctx.lineTo(x, y);
        }
        prevX = x; prevY = y;
        if (prevStage !== null && pt.stage !== prevStage) {
          // Notch tick on stage change.
          ctx.stroke();
          ctx.setLineDash([]);
          ctx.beginPath();
          ctx.moveTo(x, y - 4); ctx.lineTo(x, y + 4); ctx.stroke();
          ctx.beginPath();
          ctx.setLineDash([4, 3]);
          ctx.moveTo(x, y);
        }
        prevStage = pt.stage;
      }
      ctx.stroke();
      ctx.setLineDash([]);
    }

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
    const g = (s && s.gates) || {};
    const nss = (typeof g.next_scan_sec === "number") ? g.next_scan_sec : null;
    window.__nextScanSec = nss;
    updateNextScanLabel();
  }

  function updateNextScanLabel() {
    // v4.11.5 — always render the recycle (♻) symbol + a 2-char
    // countdown. When the backend has no schedule (e.g. weekend, or the
    // scanner hasn't started yet) we paint `♻ --` instead of falling
    // back to a counting-up `tick NNs` label. Two chars always so the
    // brand-row width budget is constant.
    const el = $("h-tick");
    if (!el) return;
    const s = window.__nextScanSec;
    if (typeof s === "number") {
      const ss = String(s).padStart(2, "0");
      el.textContent = `\u267B ${ss}s`;
      el.setAttribute("aria-label", `next scan in ${ss}s`);
      el.setAttribute("title", `next scan in ${ss}s`);
    } else {
      el.textContent = "\u267B --";
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
      // (2) Tab-heading badge — the new visible surface in v6.11.9.
      const badge = document.getElementById(`tg-badge-${name}`);
      if (badge) {
        if (enabled) {
          badge.textContent = "\u2713";
          badge.style.color = "#34d399"; // green
          badge.setAttribute(
            "title",
            `${label} executor enabled${mode ? ` (${mode} mode)` : ""}`
          );
        } else {
          badge.textContent = "\u2717";
          badge.style.color = "#9aa6b2"; // dim grey
          badge.setAttribute(
            "title",
            `${label} executor disabled (missing PAPER_KEY or *_ENABLED=0)`
          );
        }
      }
    });
    // v6.4.2 — post-loss cooldown chip + popover. Reads s.v642_flags and
    // s.active_cooldowns (both added in dashboard_server.h_state). Chip
    // is hidden entirely when the feature is disabled
    // (both sides == 0); dim when no cooldowns active; lit
    // amber with a count when 1+ (ticker, side) pairs are cooling down.
    // Each row in the popover shows TICKER side (loss $X) MM:SS left.
    // v6.4.3 — long/short windows can differ. Chip badge shows compact
    // summary: "·30m" if symmetric, "·S30m" if only short, "·L15/S30m"
    // if both differ.
    try {
      const cdChip = document.getElementById("tg-cooldown-chip");
      if (cdChip) {
        const v642 = (s && s.v642_flags) || {};
        const cdEnabled = !!v642.post_loss_cooldown_enabled;
        const cdMin = (typeof v642.post_loss_cooldown_min === "number")
          ? v642.post_loss_cooldown_min : 30;
        const cdMinLong = (typeof v642.post_loss_cooldown_min_long === "number")
          ? v642.post_loss_cooldown_min_long : cdMin;
        const cdMinShort = (typeof v642.post_loss_cooldown_min_short === "number")
          ? v642.post_loss_cooldown_min_short : cdMin;
        // v6.11.9 — always show both long and short timeouts explicitly.
        // "L:30 / S:30m" when symmetric, "L:0 / S:30m" when one side is
        // disabled. Previously we collapsed equal sides to "·30m" and
        // dropped the disabled side entirely ("·S30m"), which made it
        // look like only one timeout was active when in fact both were
        // configured — just one was set to 0. Val asked for the pill to
        // show "just time outs" so the operator can read both knobs at
        // a glance without opening the popover.
        // Badge format: "·L:<n>/S:<n>m" (zeros allowed). When BOTH sides
        // are 0 the chip stays hidden via cdEnabled=false anyway.
        let badgeText, detailText;
        const _Lstr = String(cdMinLong | 0);
        const _Sstr = String(cdMinShort | 0);
        badgeText = "\u00b7L:" + _Lstr + "/S:" + _Sstr + "m";
        if (cdMinLong === cdMinShort && cdMinLong > 0) {
          detailText = "Timeouts: long & short " + cdMinLong + " min";
        } else if (cdMinLong > 0 && cdMinShort > 0) {
          detailText = "Timeouts: long " + cdMinLong + " min, short " + cdMinShort + " min";
        } else if (cdMinShort > 0) {
          detailText = "Timeouts: long disabled, short " + cdMinShort + " min";
        } else if (cdMinLong > 0) {
          detailText = "Timeouts: long " + cdMinLong + " min, short disabled";
        } else {
          badgeText  = "\u00b7off";
          detailText = "Timeouts disabled";
        }
        const cds = (s && Array.isArray(s.active_cooldowns)) ? s.active_cooldowns : [];
        const cdCountEl = document.getElementById("tg-cooldown-count");
        const cdWinEl   = document.getElementById("tg-cooldown-window");
        const cdWinDet  = document.getElementById("tg-cooldown-window-detail");
        const cdListEl  = document.getElementById("tg-cooldown-list");
        if (!cdEnabled) {
          cdChip.style.display = "none";
        } else {
          cdChip.style.display = "inline-flex";
          if (cdCountEl) cdCountEl.textContent = String(cds.length);
          if (cdWinEl)   cdWinEl.textContent   = badgeText;
          if (cdWinDet)  cdWinDet.textContent  = detailText;
          if (cds.length > 0) {
            cdChip.style.background   = "#2a1d05";
            cdChip.style.borderColor  = "#7c5a14";
            cdChip.style.color        = "#fbbf24";
            cdChip.setAttribute("title",
              cds.length + " active post-loss cooldown" + (cds.length === 1 ? "" : "s") +
              " \u2014 " + detailText + " \u2014 click for details");
          } else {
            cdChip.style.background   = "#10151c";
            cdChip.style.borderColor  = "#1f2937";
            cdChip.style.color        = "#5b6572";
            cdChip.setAttribute("title",
              "No active post-loss cooldowns \u2014 " + detailText + " \u2014 click for details");
          }
          if (cdListEl) {
            if (cds.length === 0) {
              cdListEl.innerHTML =
                '<div style="color:#5b6572;font-style:italic">No active cooldowns.</div>';
            } else {
              cdListEl.innerHTML = cds.map(function (cd) {
                const tkr = (cd.ticker || "?").toString();
                const sd  = (cd.side || "?").toString().toUpperCase();
                const rem = Math.max(0, cd.remaining_sec | 0);
                const mm  = Math.floor(rem / 60);
                const ss  = rem % 60;
                const remStr = (mm < 10 ? "0" : "") + mm + ":" + (ss < 10 ? "0" : "") + ss;
                const loss = (typeof cd.loss_pnl === "number")
                  ? "$" + cd.loss_pnl.toFixed(2) : "";
                const sdColor = sd === "LONG" ? "#34d399" : "#f87171";
                return '<div style="display:flex;align-items:center;justify-content:space-between;'
                     + 'padding:4px 6px;background:#10151c;border:1px solid #1f2937;border-radius:4px">'
                     + '<span><span style="color:#e7ecf3;font-weight:600">' + tkr + '</span> '
                     + '<span style="color:' + sdColor + ';font-size:10px;margin-left:4px">' + sd + '</span></span>'
                     + '<span style="display:flex;align-items:center;gap:8px">'
                     + '<span style="color:#f87171;font-size:10px">' + loss + '</span>'
                     + '<span style="color:#fbbf24;font-variant-numeric:tabular-nums">' + remStr + '</span>'
                     + '</span></div>';
              }).join("");
            }
          }
        }
      }
      // Wire chip toggle once.
      if (!window.__tgCooldownPopWired) {
        window.__tgCooldownPopWired = true;
        const chip = document.getElementById("tg-cooldown-chip");
        const pop  = document.getElementById("tg-cooldown-pop");
        const close = document.getElementById("tg-cooldown-close");
        if (chip && pop) {
          chip.addEventListener("click", function (e) {
            e.stopPropagation();
            const open = pop.style.display !== "none";
            pop.style.display = open ? "none" : "block";
            chip.setAttribute("aria-expanded", open ? "false" : "true");
          });
        }
        if (close && pop) {
          close.addEventListener("click", function () {
            pop.style.display = "none";
            if (chip) chip.setAttribute("aria-expanded", "false");
          });
        }
        document.addEventListener("click", function (e) {
          if (!pop || pop.style.display === "none") return;
          if (pop.contains(e.target)) return;
          if (chip && chip.contains(e.target)) return;
          pop.style.display = "none";
          if (chip) chip.setAttribute("aria-expanded", "false");
        });
      }
    } catch (e) { /* dashboard chip is best-effort */ }

    // v4.2.2 — extract tz token (ET/CDT/CT/PT/PST/\u2026) from
    // server_time_label tail, e.g. "Fri Apr 24 · 13:09:13 ET".
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

  function renderAll(s) {
    if (!s || !s.ok) return;
    lastSnapshot = s;
    // Publish latest state so the executor-tab IIFE can read market-wide
    // widgets (proximity, regime, gates) from the same source as Main.
    try {
      window.__tgLastState = s;
      if (typeof window.__tgOnState === "function") window.__tgOnState(s);
    } catch (e) {}
    const sl = paperSlice(s);
    renderHeader(s);
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
    try { renderWeatherCheck(s); } catch (e) { /* never break Main */ }
    try { renderPermitMatrix(s); } catch (e) { /* never break Main */ }
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
      showBanner("Live stream dropped. Reconnecting… data is polled every 5s.", "warn");
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
    // v4.11.5 — header tick is always a countdown to next scan. When
    // the backend reports a number we decrement it once per second; when
    // it doesn't, updateNextScanLabel() paints `♻ --` (still 2 chars)
    // instead of the old counting-up `tick NNs` fallback. The 1s tick
    // also keeps the label fresh after a /state refresh resets the
    // value.
    streamTickTimer = setInterval(() => {
      if (typeof window.__nextScanSec === "number") {
        window.__nextScanSec = Math.max(0, window.__nextScanSec - 1);
      }
      updateNextScanLabel();
    }, 1000);

    streamConn.addEventListener("state", (ev) => {
      lastDataAt = Date.now();
      setConn("live");
      try { renderAll(JSON.parse(ev.data).data); } catch (e) {}
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
    pollTimer = setInterval(pollOnce, 5000);
  }
  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }
  function scheduleReconnect() {
    scheduleStreamReconnect(4000);
  }

  // v4.2.2 — client-side 1Hz clock tick. Renders HH:MM:SS + tz
  // label (e.g. "13:09:13 ET") in the row-2 clock. Uses browser local
  // time so seconds advance smoothly; the tz token comes from the
  // server's server_time_label tail, cached in window.__tgClockTz.
  // If we haven't seen a server label yet, we render just HH:MM:SS.
  window.__tgTickClock = function () {
    const el = document.getElementById("tg-brand-clock");
    if (!el) return;
    const d = new Date();
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    const ss = String(d.getSeconds()).padStart(2, "0");
    const tz = window.__tgClockTz || "";
    // v4.3.1 — drop seconds on very narrow phones (<=360px) so
    // the HH:MM TZ label fits inline with logo/version/LIVE pill.
    // v6.0.7 — extend to <=480px (covers iPhone 13/14/15 standard
    // 390 AND iPhone Pro Max 430). At 430 the brand row overflowed
    // by ~70px (body scrollWidth=500, viewport=430) which pushed
    // every page card to the right and clipped Today's Trades and
    // the Permit Matrix STRIKES/State columns. Dropping the :SS
    // segment recovers ~21px and brings the row inside the viewport.
    const narrow = window.matchMedia && window.matchMedia("(max-width: 480px)").matches;
    const t = narrow ? `${hh}:${mm}` : `${hh}:${mm}:${ss}`;
    el.textContent = tz ? `${t} ${tz}` : t;
  };
  setInterval(window.__tgTickClock, 1000);
  window.__tgTickClock();

  // stale-data watchdog: if no data in 10s, drop to polling.
  // Uses scheduleStreamReconnect so back-to-back watchdog ticks
  // can't queue multiple reconnect setTimeouts.
  setInterval(() => {
    if (lastDataAt && (Date.now() - lastDataAt) > 10000 && streamConn) {
      setConn("polling");
      stopStream();
      startPolling();
      scheduleStreamReconnect(15000);
    }
  }, 3000);

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
    window.__tgRenderWeatherCheck = renderWeatherCheck;
    window.__tgRenderPermitMatrix = renderPermitMatrix;
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
    const parts = rows.map(r => {
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
      // v4.12.0 — after-hours layer. Backend tags r.ah=true outside RTH
      // when the latest trade differs from the relevant base close. We
      // append a small `AH` badge plus the AH delta so the user can
      // tell at a glance how much the index has moved since the close.
      let ahHtml = "";
      if (r.ah && r.ah_change !== null && r.ah_change !== undefined) {
        const ahUp = r.ah_change >= 0;
        const ahColor = ahUp ? "#34d399" : "#f87171";
        const ahSign = ahUp ? "+" : "";
        const ahChg = ahSign + fmtNum(r.ah_change, 2);
        const ahPct = (r.ah_change_pct === null || r.ah_change_pct === undefined)
          ? "" : ` ${ahSign}${fmtNum(r.ah_change_pct, 2)}%`;
        const sessLabel = session === "pre" ? "PRE" : "AH";
        ahHtml = ` <span class="idx-ah" title="After-hours move vs close">${sessLabel} <span style="color:${ahColor};font-weight:500">${ahChg}${ahPct}</span></span>`;
      }
      // v4.13.0 — inline futures badge for cash indices. Reuses the
      // .idx-ah class for consistent styling, painted in the future's own
      // direction color so green/red read independently of the cash row.
      let futHtml = "";
      if (r.future && r.future.change_pct !== null && r.future.change_pct !== undefined) {
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

  <main class="main">

    <div class="banner hide" data-f="banner"></div>

    <section class="kpi-row kpi-row-4">
      <div class="kpi"><span class="kpi-label">Equity</span><span class="kpi-value" data-f="k-equity">\u2014</span><span class="kpi-sub" data-f="k-equity-sub">\u2014</span></div>
      <div class="kpi"><span class="kpi-label">Day P&amp;L</span><span class="kpi-value" data-f="k-pnl">\u2014</span><span class="kpi-sub" data-f="k-pnl-sub">\u2014</span></div>
      <div class="kpi"><span class="kpi-label">Open</span><span class="kpi-value" data-f="k-open">\u2014</span><span class="kpi-sub" data-f="k-open-sub">\u2014</span></div>
      <div class="kpi"><span class="kpi-label">Session</span><span class="kpi-value" data-f="k-session" style="font-size:20px">\u2014</span><span class="kpi-sub" data-f="k-session-sub">\u2014</span></div>
    </section>

    <!-- v5.20.0 \u2014 Open positions sits ABOVE the Weather Check banner so currently-held risk
         is visible first; the Weather banner (a conditional "can I take a new entry?" verdict)
         appears immediately below. Mirrors the Main panel reorder shipped in v5.19.4. -->
    <section class="grid">
      <div class="card">
        <div class="card-head"><span class="card-title">Open positions<span class="count" data-f="pos-count">\u00b7 0</span></span></div>
        <div class="card-body flush" data-f="pos-body">
          <div class="empty">No open positions.</div>
        </div>
        <div data-f="port-strip" style="display:none">
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:0;border-top:1px solid var(--border);background:var(--surface-2)">
            <div style="padding:10px 14px;border-right:1px solid var(--border)">
              <div style="font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--text-dim);margin-bottom:4px">Equity</div>
              <div class="mono" style="font-size:15px;color:var(--text)" data-f="port-equity">\u2014</div>
            </div>
            <div style="padding:10px 14px">
              <div style="font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--text-dim);margin-bottom:4px">Buying power</div>
              <div class="mono" style="font-size:15px;color:var(--text)" data-f="port-bp">\u2014</div>
            </div>
          </div>
          <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:0;border-top:1px solid var(--border);background:var(--surface-2)">
            <div style="padding:8px 14px;border-right:1px solid var(--border)">
              <div style="font-size:9.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--text-dim);margin-bottom:2px">Cash</div>
              <div class="mono" style="font-size:12px;color:var(--text-muted)" data-f="port-cash">\u2014</div>
            </div>
            <div style="padding:8px 14px;border-right:1px solid var(--border)">
              <div style="font-size:9.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--text-dim);margin-bottom:2px">Invested</div>
              <div class="mono" style="font-size:12px;color:var(--text-muted)" data-f="port-longmv">\u2014</div>
            </div>
            <div style="padding:8px 14px">
              <div style="font-size:9.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--text-dim);margin-bottom:2px">Shorted</div>
              <div class="mono" style="font-size:12px;color:var(--down)" data-f="port-shortliab">\u2014</div>
            </div>
          </div>
        </div>
      </div>
    </section>

    <!-- v5.20.0 \u2014 Weather Check banner (Phase 1 Sovereign verdict) sits BELOW Open positions
         so the operator sees held risk first and the new-entry permit second. -->
    <section class="pmtx-weather-section" aria-label="Phase 1 weather check">
      <div class="pmtx-weather pmtx-weather-pending" data-f="pmtx-weather">
        <div class="pmtx-weather-icon" data-f="pmtx-weather-icon" aria-hidden="true">\u00B7</div>
        <div class="pmtx-weather-body">
          <div class="pmtx-weather-eyebrow">Weather check \u00b7 Phase 1 Sovereign</div>
          <div class="pmtx-weather-verdict" data-f="pmtx-weather-verdict">Waiting for permit state\u2026</div>
          <div class="pmtx-weather-detail" data-f="pmtx-weather-detail">QQQ 5m close vs 9-EMA \u00b7 QQQ vs AVWAP_0930</div>
        </div>
        <div class="pmtx-weather-stats" data-f="pmtx-weather-stats" aria-hidden="true"></div>
      </div>
    </section>

    <section class="grid">
      <div class="card">
        <div class="card-head">
          <span class="card-title" title="Per-Titan view of the Tiger Sovereign v15.0 entry checklist (market-wide \u2014 same data as Main).">Permit Matrix<span class="count" data-f="pmtx-count">\u00b7 \u2014</span></span>
          <span class="chip" data-f="pmtx-overall-chip" title="Aggregate Phase 1 permit state across long and short">\u2014</span>
        </div>
        <div class="card-body flush" data-f="pmtx-body">
          <div class="empty">Waiting for matrix data\u2026</div>
        </div>
      </div>
    </section>

    <section class="grid">
      <div class="card">
        <div class="card-head"><span class="card-title">Today's trades<span class="count" data-f="trades-count">\u00b7 \u2014</span></span><span class="chip" data-f="trades-realized">\u2014</span></div>
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
    }
    return panel;
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

  async function pollExecutor(name) {
    const panel = ensureExecSkeleton(name);
    if (!panel) return;
    try {
      const r = await fetch("/api/executor/" + name, { credentials: "same-origin" });
      if (!r.ok) throw new Error("http " + r.status);
      const data = await r.json();
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
    // The executor's mode (Live vs Paper) is still surfaced via the
    // tooltip/title so it's one click away without crowding the tab
    // strip. Replaces the previous "🟢 Live" / "📄 Paper" / "off" labels.
    // renderHeader() also writes this badge from s.executors_status as
    // a faster initial paint; this per-executor poll keeps it accurate
    // for executors that go offline mid-session.
    const el = $$("tg-badge-" + name);
    if (!el) return;
    const label = name.charAt(0).toUpperCase() + name.slice(1);
    if (!data || data.enabled === false) {
      el.textContent = "\u2717";
      el.style.color = "#9aa6b2";
      el.setAttribute("title",
        `${label} executor disabled (missing PAPER_KEY or *_ENABLED=0)`);
      return;
    }
    el.textContent = "\u2713";
    el.style.color = "#34d399";
    const mode = (data.mode === "live") ? "live" : "paper";
    el.setAttribute("title", `${label} executor enabled (${mode} mode)`);
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
  function renderExecTrades(panel, data, disabled) {
    const body = execField(panel, "trades-body");
    const count = execField(panel, "trades-count");
    const chip = execField(panel, "trades-realized");
    const trades = (data && Array.isArray(data.todays_trades)) ? data.todays_trades : [];
    if (count) count.textContent = "\u00b7 " + (disabled ? "\u2014" : trades.length);

    // Chip: running realized P&L (only SELL rows may carry pnl).
    let realized = 0, havePnl = 0;
    for (const t of trades) {
      if (typeof t.pnl === "number" && isFinite(t.pnl)) { realized += t.pnl; havePnl += 1; }
    }
    if (chip) {
      if (disabled || !havePnl) {
        chip.textContent = "\u2014";
        chip.className = "chip chip-neut";
      } else {
        chip.textContent = fmtUsd(realized);
        chip.className = "chip " + (realized > 0 ? "chip-ok" : (realized < 0 ? "chip-down" : "chip-neut"));
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
      const isBuy = act === "BUY";
      const isSell = act === "SELL";
      const sym = t.ticker || t.symbol || "\u2014";
      const shares = t.shares ?? t.qty;
      const px = t.price ?? t.avg_fill_price ?? t.entry_price ?? t.exit_price;
      const actCls = isSell ? "act-sell" : "act-buy";
      const actLbl = act || "\u2014";
      let tailHtml = "\u2014";
      if (isBuy) {
        const cost = (typeof t.cost === "number" && isFinite(t.cost))
          ? t.cost
          : ((typeof shares === "number" && typeof px === "number") ? shares * px : null);
        tailHtml = cost !== null
          ? `<span class="trade-cost">${fmtUsd(cost)}</span>`
          : `<span class="trade-cost">\u2014</span>`;
      } else if (isSell) {
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
    renderBadge(name, data);
    const panel = ensureExecSkeleton(name);
    if (!panel) return;
    const label = name === "val" ? "Val" : "Gene";
    const disabled = !data || data.enabled === false;

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
    const portStrip = execField(panel, "port-strip");
    const posCount = execField(panel, "pos-count");
    if (posCount) posCount.textContent = "\u00b7 " + (disabled ? "\u2014" : positions.length);

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
          // v6.0.6 \u2014 chandelier stage >= 1 also counts as armed trail
          // (Alarm F never sets the legacy trail_active flag).
          const _chStage = Number(_mp.chandelier_stage) || 0;
          const _trailArmed = !!_mp.trail_active || _chStage >= 1;
          _stopBySym[_mp.ticker] = { eff: _eff, trail: _trailArmed };
        }
        const rows = positions.map(p => {
          const upok = (Number(p.unrealized_pnl) || 0) >= 0;
          const color = upok ? "var(--up)" : "var(--down)";
          const sideCls = p.side === "SHORT" ? "side-short" : "side-long";
          const _stopInfo = _stopBySym[p.symbol] || null;
          let _stopTxt = "\u2014";
          if (_stopInfo && Number.isFinite(_stopInfo.eff)) {
            _stopTxt = fmtNum(_stopInfo.eff, 2);
            if (_stopInfo.trail) {
              _stopTxt += ` <span class="trail-badge" title="Trail stop is armed">TRAIL</span>`;
            }
          }
          return `<tr>
            <td style="padding:6px 10px">${esc(p.symbol)}</td>
            <td style="padding:6px 10px" class="${sideCls}">${esc(p.side)}</td>
            <td class="mono" style="padding:6px 10px;text-align:right">${fmtNum(p.qty, 0)}</td>
            <td class="mono" style="padding:6px 10px;text-align:right">${fmtNum(p.avg_entry, 2)}</td>
            <td class="mono" style="padding:6px 10px;text-align:right">${fmtNum(p.current_price, 2)}</td>
            <td class="mono" style="padding:6px 10px;text-align:right">${_stopTxt}</td>
            <td class="mono" style="padding:6px 10px;text-align:right;color:${color}">${fmtUsd(p.unrealized_pnl)}</td>
            <td class="mono" style="padding:6px 10px;text-align:right;color:${color}">${fmtPctExec(p.unrealized_pnl_pct, 2)}</td>
          </tr>`;
        }).join("");
        posBody.innerHTML = `
          <table style="width:100%;border-collapse:collapse;font-size:12.5px">
            <thead><tr style="color:var(--text-dim);text-transform:uppercase;font-size:10px;letter-spacing:.08em;border-bottom:1px solid var(--border)">
              <th style="text-align:left;padding:8px 10px">Ticker</th>
              <th style="text-align:left;padding:8px 10px">Side</th>
              <th style="text-align:right;padding:8px 10px">Qty</th>
              <th style="text-align:right;padding:8px 10px">Avg Entry</th>
              <th style="text-align:right;padding:8px 10px">Mark</th>
              <th style="text-align:right;padding:8px 10px" title="Effective stop from the engine (Main state). TRAIL badge means the trail stop is armed.">Stop</th>
              <th style="text-align:right;padding:8px 10px">Unrealized</th>
              <th style="text-align:right;padding:8px 10px">%</th>
            </tr></thead>
            <tbody>${rows}</tbody>
          </table>`;
      }
    }

    // Cash / BP / Invested / Shorted footer under positions card ------
    if (portStrip) {
      portStrip.style.display = disabled ? "none" : "";
      setField(panel, "port-equity", equity === null ? "\u2014" : fmtUsd(equity));
      setField(panel, "port-bp", bp === null ? "\u2014" : fmtUsd(bp));
      setField(panel, "port-cash", cash === null ? "\u2014" : fmtUsd(cash));
      setField(panel, "port-longmv", fmtUsd(invested));
      setField(panel, "port-shortliab", fmtUsd(shorted));
    }

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
            const label = (r.ticker || "?") + " " + (r.side || "") + " " +
              (r.entry_ts_utc || "") + " (" + (r.status || "") + ")";
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
          '  <span style="font-size:10.5px;color:var(--text-dim);font-family:monospace" title="Event timestamp in UTC">' + escHtml(ev.event_ts_utc || "") + '</span>' +
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
})();
