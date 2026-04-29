(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  // v4.1.8-dash \u2014 Robinhood view was removed in v3.5.0 along with the
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

  // v4.10.0 — shared GATE renderer. Used by Main + Val/Gene exec panels.
  // Sets text + class. Caller passes the value cell, the sub cell, the
  // gates obj from /api/state, and the regime obj (for market_open
  // inference via mode === "CLOSED").
  const __GATE_CLASSES = ["gate-armed", "gate-paused", "gate-after-hours", "gate-halted"];
  function applyGateTriState(gateEl, gateSubEl, gates, regime) {
    if (!gateEl) return;
    gateEl.style.color = "";
    __GATE_CLASSES.forEach(c => gateEl.classList.remove(c));
    const mode = (regime && regime.mode) || "";
    const marketClosed = mode === "CLOSED";
    if (gates.trading_halted) {
      gateEl.textContent = "HALTED";
      gateEl.classList.add("gate-halted");
      if (gateSubEl) gateSubEl.textContent = gates.halt_reason || "manual halt";
    } else if (marketClosed) {
      gateEl.textContent = "AFTER HOURS";
      gateEl.classList.add("gate-after-hours");
      if (gateSubEl) gateSubEl.textContent = "market closed";
    } else if (gates.scan_paused) {
      gateEl.textContent = "PAUSED";
      gateEl.classList.add("gate-paused");
      if (gateSubEl) gateSubEl.textContent = "scan paused";
    } else if (!gates.or_collected_date) {
      gateEl.textContent = "WAIT";
      gateEl.classList.add("gate-paused");
      if (gateSubEl) gateSubEl.textContent = "opening range not collected";
    } else {
      gateEl.textContent = "ARMED";
      gateEl.classList.add("gate-armed");
      if (gateSubEl) gateSubEl.textContent = `OR ${gates.or_collected_date}`;
    }
  }
  // v4.10.2 — expose to the second IIFE (Val/Gene tabs). The whole
  // file is two independent IIFEs (main tab is 1–797; tab switcher
  // + per-executor poll is 799–1632). v4.10.0 added the helper to
  // the first IIFE only, so renderExecutor()/refreshExecSharedKpis()
  // in the second IIFE threw "applyGateTriState is not defined" the
  // moment a Val/Gene poll landed (caught by pollExecutor and surfaced
  // as the "Fetch failed: ..." red banner). Bridge via window so the
  // two IIFEs stay otherwise independent (per the design comment at
  // line 800).
  window.__tgApplyGateTriState = applyGateTriState;

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

    const gates = s.gates || {};
    const gateEl = $("k-gate");
    // v4.10.0 — GATE tri-state. Before this, "PAUSED" amber was shown
    // 24/7 outside RTH because scan_paused is the union of /pause and
    // the auto-idle "no scan outside market hours" flag. We now infer
    // after-hours from regime.mode === "CLOSED" (option (b) in the
    // v4.10.0 spec) so the operator sees:
    //   ARMED       — green   — market open + scanner ready
    //   AFTER HOURS — muted   — market closed (no scanning expected)
    //   PAUSED      — amber   — operator paused via /pause during RTH
    //   HALTED      — red     — emergency halt (existing behavior)
    //   WAIT        — amber   — opening range still being collected
    applyGateTriState(gateEl, $("k-gate-sub"), gates, s.regime);

    const reg = s.regime || {};
    // ── Regime KPI: directional market regime (BULLISH / NEUTRAL / BEARISH)
    //    sourced from breadth (SPY/QQQ vs AVWAP), to match how the bot talks about regime
    const rEl = $("k-regime");
    const breadth = reg.breadth || "UNKNOWN";
    rEl.textContent = breadth === "UNKNOWN" ? "—" : breadth;
    rEl.style.color = breadth === "BULLISH" ? "var(--up)"
                    : breadth === "BEARISH" ? "var(--down)"
                    : breadth === "NEUTRAL" ? "var(--text)"
                    : "var(--text-muted)";
    $("k-regime-sub").textContent = `RSI ${reg.rsi_regime || "—"}`;

    // ── Session KPI: time-of-day / risk state (OPEN / CHOP / POWER / DEFENSIVE / CLOSED)
    //    this is MarketMode in the bot — a session window, not a directional view
    const sEl = $("k-session");
    const mode = reg.mode || "—";
    sEl.textContent = mode;
    sEl.style.color = mode === "DEFENSIVE" ? "var(--down)"
                    : mode === "CHOP" ? "var(--warn)"
                    : mode === "CLOSED" ? "var(--text-muted)"
                    : mode === "—" ? "var(--text-muted)"
                    : "var(--up)";
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
        const trailBadge = p.trail_active
          ? ` <span class="trail-badge" title="Trail armed — effective stop is trail_stop, not hard stop">TRAIL</span>`
          : "";
        // v5.10.6 \u2014 Phase badge + Sovereign Brake distance. Phase
        // mirrors pos["phase"] (A/B/C). SB distance = unrealized + 500;
        // green > $200 (breathing room), yellow $50\u2013$200 (close),
        // red < $50 (about to trip).
        const phase = (p.phase === "B" || p.phase === "C") ? p.phase : "A";
        const phaseBadge = `<span class="eot-phase-badge eot-phase-${phase}" title="v5.10 Phase ${phase}">${phase}</span>`;
        const sb = p.sovereign_brake_distance_dollars;
        let sbCls = "eot-sb-green", sbLabel = "—";
        if (typeof sb === "number" && isFinite(sb)) {
          if (sb < 50) sbCls = "eot-sb-red";
          else if (sb < 200) sbCls = "eot-sb-yellow";
          sbLabel = (sb >= 0 ? "+" : "−") + "$" + Math.abs(sb).toFixed(0);
        }
        return `<tr>
          <td><span class="ticker">${escapeHtml(p.ticker)} <span class="mark ${markCls}">●</span></span>${phaseBadge}</td>
          <td><span class="${sideCls}">${p.side}</span></td>
          <td class="right">${p.shares}</td>
          <td class="right">${fmtPx(p.entry)}</td>
          <td class="right">${fmtPx(p.mark)}</td>
          <td class="right">${fmtPx(eff)}${trailBadge}</td>
          <td class="right ${pnlCls}">${fmtUsd(p.unrealized)}</td>
          <td class="right ${sbCls}" title="Sovereign Brake distance — fires at -$500 unrealized">${sbLabel}</td>
        </tr>`;
      }).join("");
      body.innerHTML = `<table>
        <thead><tr>
          <th>Ticker</th><th>Side</th>
          <th class="right">Sh</th><th class="right">Entry</th>
          <th class="right">Mark</th><th class="right">Stop</th>
          <th class="right">Unreal.</th>
          <th class="right" title="Sovereign Brake distance">SB Δ</th>
        </tr></thead>
        <tbody>${rows}</tbody></table>`;
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

  function renderProximity(s) {
    const rows = s.proximity || [];
    $("prox-count").textContent = `· ${rows.length} tracked`;
    const list = $("prox-list");
    if (!rows.length) {
      list.innerHTML = `<div class="empty">No tickers configured.</div>`;
      return;
    }
    const html = rows.map((r) => {
      const pct = (r.nearest_pct !== null && r.nearest_pct !== undefined) ? r.nearest_pct : null;
      // Convert "distance" (0 = touching) to a bar width where closer = longer bar.
      // Clamp: 0% distance → 100% bar; 2% distance → 0% bar.
      let fill = 0;
      if (pct !== null) fill = Math.max(0, Math.min(100, Math.round((1 - Math.min(pct, 0.02) / 0.02) * 100)));
      const warn = pct !== null && pct < 0.005;
      const mark = r.open_side === "SHORT"
        ? '<span class="mark mark-short" title="Open short">●</span>'
        : r.open_side === "LONG"
          ? '<span class="mark mark-long" title="Open long">●</span>'
          : "";
      const lbl = r.nearest_label || "—";
      const pctText = pct !== null ? `${(pct * 100).toFixed(2)}% · ${lbl}` : "—";
      // v5.13.4 — permit-side chip (Phase 1 entry-relevant boundary).
      const permitSide = r.permit_side || "NONE";
      const permitChip = renderPermitSideChip(permitSide);
      const dim = (permitSide === "NONE") ? ' style="opacity:0.55"' : '';
      return `<div class="prox-row"${dim}>
        <span class="prox-ticker">${escapeHtml(r.ticker)} ${mark}</span>
        <span class="prox-price">${fmtPx(r.price)}</span>
        <div class="prox-bar"><div class="prox-fill ${warn ? "warn" : "ok"}" style="width:${fill}%"></div></div>
        <span class="prox-pct" style="color:${warn ? 'var(--warn)' : 'var(--text-muted)'}">${pctText} ${permitChip}</span>
      </div>`;
    }).join("");
    list.innerHTML = html;
  }

  function renderPermitSideChip(permitSide) {
    if (permitSide === "BOTH") return `<span class="tgate-chip on" title="Phase 1 permit: long + short">L+S</span>`;
    if (permitSide === "LONG") return `<span class="tgate-chip on" title="Phase 1 long permit active">L</span>`;
    if (permitSide === "SHORT") return `<span class="tgate-chip on" title="Phase 1 short permit active">S</span>`;
    return `<span class="tgate-chip na" title="No Phase 1 permit \u2014 breakout would not fire">no permit</span>`;
  }

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
        sumEl.innerHTML = '<span class="ts-seg">No fills yet today.</span>';
      } else {
        const realCls = summary.have_pnl === 0 ? "na"
                      : (summary.realized > 0 ? "up" : (summary.realized < 0 ? "down" : ""));
        const realTxt = summary.have_pnl === 0 ? "—" : fmtUsd(summary.realized);
        const wrTxt   = summary.win_rate === null ? "—"
                      : (Math.round(summary.win_rate * 100) + "%");
        sumEl.innerHTML =
          `<span class="ts-seg"><span class="ts-val">${summary.opens}</span> open${summary.opens===1?"":"s"}</span>` +
          `<span class="ts-seg"><span class="ts-val">${summary.closes}</span> close${summary.closes===1?"":"s"}</span>` +
          `<span class="ts-seg">realized <span class="ts-val ${realCls}">${realTxt}</span></span>` +
          `<span class="ts-seg">win <span class="ts-val">${wrTxt}</span></span>`;
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
      // v5.5.7 \u2014 classify by open vs close, not strictly BUY/SELL.
      // SHORT entries pair with COVER exits; treating only BUY/SELL as
      // tradable actions hid realized pnl on COVER rows.
      const isOpen  = (act === "BUY" || act === "SHORT");
      const isClose = (act === "SELL" || act === "COVER");
      const side  = t.side || "LONG";
      const sym   = t.ticker || "—";
      const shares = t.shares;
      const px    = t.price ?? t.entry_price ?? t.exit_price;

      // Action chip \u2014 open (green) / close (red). Symbol still
      // carries LONG/SHORT colour coding to avoid double-cueing.
      const actCls = isClose ? "act-sell" : "act-buy";
      const actLbl = act || (side === "SHORT" ? "SHORT" : "LONG");

      // v4.2.1 \u2014 tail column (between action and unit price):
      //   open  \u2192 total cost, subdued
      //   close \u2192 signed pnl + matching-colour pnl %
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

  function renderObserver(s) {
    // Observer card holds the *details* behind the Regime KPI — the
    // KPI itself shows BULLISH/NEUTRAL/BEARISH and RSI label; this card
    // adds the underlying numbers (SPY/QQQ vs AVWAP, RSI values).
    const reg = s.regime || {};
    const obs = s.observer || {};
    $("obs-breadth").textContent = reg.breadth_detail || reg.breadth || "—";
    $("obs-rsi").textContent = reg.rsi_detail || reg.rsi_regime || "—";
    const red = obs.ticker_red || [];
    if (red.length) {
      const [tkr, pnl] = red[0];
      $("obs-topred").textContent = `${tkr} ${fmtUsd(pnl)}`;
    } else {
      $("obs-topred").textContent = "—";
    }
  }

  // v5.13.2 \u2014 Sovereign Regime Shield panel retired (PDC dual-index
  // eject rule was decommissioned in v5.9.1). The renderer is gone;
  // the shared `regime.sovereign` payload remains in /api/state for
  // any external consumers but is no longer drawn on the dashboard.

  // v5.13.2 \u2014 Tiger Sovereign Phase 1\u20134 renderer. Replaces the
  // v5.10.6 "Eye of the Tiger" panel with the spec-correct Phase
  // surface. Reads `state.tiger_sovereign` directly. All cells are
  // None-safe \u2014 missing fields render as em-dashes.
  function _tsCheck(ok) {
    if (ok === true) return '<span class="ts-ok">\u2713</span>';
    if (ok === false) return '<span class="ts-no">\u2717</span>';
    return '<span class="ts-pending">\u2014</span>';
  }
  function _tsNum(v, digits) {
    if (v === null || v === undefined || !isFinite(v)) return "\u2014";
    return Number(v).toFixed(digits === undefined ? 2 : digits);
  }
  function _tsMoney(v) {
    if (v === null || v === undefined || !isFinite(v)) return "\u2014";
    const n = Number(v);
    const sign = n >= 0 ? "+" : "\u2212";
    return sign + "$" + Math.abs(n).toFixed(2);
  }
  function _tsPermitRow(label, ok, leftValue, rightValue, leftLabel, rightLabel) {
    return '<div class="ts-row">'
      + '<span class="ts-row-label">' + _tsCheck(ok) + ' ' + escapeHtml(label) + '</span>'
      + '<span class="ts-row-value">'
      +   '<span class="ts-row-pair">' + escapeHtml(leftLabel) + ' '
      +     '<span class="mono">' + escapeHtml(leftValue) + '</span></span>'
      +   '<span class="ts-row-pair">' + escapeHtml(rightLabel) + ' '
      +     '<span class="mono">' + escapeHtml(rightValue) + '</span></span>'
      + '</span>'
      + '</div>';
  }
  function _tsRenderPhase1Side(bodyId, side, blk) {
    const body = $(bodyId);
    if (!body) return false;
    if (!blk || Object.keys(blk).length === 0) {
      body.innerHTML = '<div class="empty">Waiting for permit state\u2026</div>';
      return false;
    }
    const close = (typeof blk.qqq_5m_close === "number") ? blk.qqq_5m_close : null;
    const ema9 = (typeof blk.qqq_5m_ema9 === "number") ? blk.qqq_5m_ema9 : null;
    const last = (typeof blk.qqq_last === "number") ? blk.qqq_last : null;
    const avwap = (typeof blk.qqq_avwap_0930 === "number") ? blk.qqq_avwap_0930 : null;
    let okEma = null, okAvwap = null;
    if (close !== null && ema9 !== null) {
      okEma = (side === "LONG") ? (close > ema9) : (close < ema9);
    }
    if (last !== null && avwap !== null) {
      okAvwap = (side === "LONG") ? (last > avwap) : (last < avwap);
    }
    const cmpEma = (side === "LONG") ? "> EMA9" : "< EMA9";
    const cmpAvwap = (side === "LONG") ? "> AVWAP_0930" : "< AVWAP_0930";
    const permit = !!blk.permit;
    const permitCls = permit ? "ts-permit-green" : "ts-permit-red";
    const permitTxt = permit ? "GREEN" : "RED";
    body.innerHTML = ''
      + _tsPermitRow("QQQ 5m close " + cmpEma, okEma,
          _tsNum(close), _tsNum(ema9), "close=", "ema9=")
      + _tsPermitRow("QQQ " + cmpAvwap, okAvwap,
          _tsNum(last), _tsNum(avwap), "last=", "avwap=")
      + '<div class="ts-row ts-permit-line">'
      +   '<span class="ts-row-label">Permit</span>'
      +   '<span class="ts-row-value"><span class="ts-permit-pill ' + permitCls + '">'
      +     escapeHtml(permitTxt) + '</span></span>'
      + '</div>';
    return permit;
  }
  function _tsRenderPhase2(blk) {
    const body = $("ts-phase2-body");
    if (!body) return;
    const rows = Array.isArray(blk) ? blk : [];
    if (rows.length === 0) {
      body.innerHTML = '<div class="empty">Waiting for gate state\u2026</div>';
      return;
    }
    const html = rows.map((r) => {
      const status = String(r.vol_gate_status || "COLD");
      let cls = "ts-gate-cold";
      if (status === "PASS") cls = "ts-gate-pass";
      else if (status === "FAIL") cls = "ts-gate-fail";
      else if (status === "OFF") cls = "ts-gate-off";
      const above = !!r.two_consec_above;
      const below = !!r.two_consec_below;
      let twoLabel = "\u2014", twoIcon = _tsCheck(null);
      if (above) { twoLabel = "above OR_high"; twoIcon = _tsCheck(true); }
      else if (below) { twoLabel = "below OR_low"; twoIcon = _tsCheck(true); }
      else { twoLabel = "no 2-consec"; twoIcon = _tsCheck(false); }
      return '<div class="ts-row">'
        + '<span class="ts-row-label"><span class="ticker">' + escapeHtml(r.ticker || "") + '</span></span>'
        + '<span class="ts-row-value">'
        +   '<span class="ts-vol-pill ' + cls + '" title="Volume gate">' + escapeHtml(status) + '</span>'
        +   '<span class="ts-row-pair">' + twoIcon + ' ' + escapeHtml(twoLabel) + '</span>'
        + '</span>'
        + '</div>';
    }).join("");
    body.innerHTML = html;
  }
  function _tsRenderPhase3(blk) {
    const body = $("ts-phase3-body");
    if (!body) return;
    const rows = Array.isArray(blk) ? blk : [];
    if (rows.length === 0) {
      body.innerHTML = '<div class="empty">No armed candidates.</div>';
      return;
    }
    const html = rows.map((r) => {
      const e1Fired = !!r.entry1_fired;
      const e2Fired = !!r.entry2_fired;
      const e2Pending = !!r.entry2_cross_pending;
      const di = (typeof r.entry1_di === "number") ? r.entry1_di : null;
      const nhodTxt = (r.entry1_nhod === true) ? "NHOD\u2713"
        : (r.entry1_nhod === false ? "no NHOD" : "NHOD \u2014");
      const e2Txt = e2Fired
        ? "Entry 2 \u2713"
        : (e2Pending ? "Entry 2 cross pending" : "Entry 2 \u2014");
      return '<div class="ts-row">'
        + '<span class="ts-row-label">'
        +   '<span class="ticker">' + escapeHtml(r.ticker || "") + '</span> '
        +   '<span class="ts-side ts-side-' + (r.side === "SHORT" ? "short" : "long") + '">'
        +     escapeHtml(r.side || "\u2014") + '</span>'
        + '</span>'
        + '<span class="ts-row-value">'
        +   '<span class="ts-row-pair">' + _tsCheck(e1Fired) + ' Entry 1</span>'
        +   '<span class="ts-row-pair mono">DI=' + escapeHtml(_tsNum(di, 2)) + '</span>'
        +   '<span class="ts-row-pair">' + escapeHtml(nhodTxt) + '</span>'
        +   '<span class="ts-row-pair">' + escapeHtml(e2Txt) + '</span>'
        + '</span>'
        + '</div>';
    }).join("");
    body.innerHTML = html;
  }
  function _tsRenderPhase4(blk) {
    const body = $("ts-phase4-body");
    if (!body) return;
    const rows = Array.isArray(blk) ? blk : [];
    if (rows.length === 0) {
      body.innerHTML = '<div class="empty">No open positions.</div>';
      return;
    }
    const html = rows.map((r) => {
      const sen = r.sentinel || {};
      const tg = r.titan_grip || {};
      const a1 = (typeof sen.a1_pnl === "number") ? sen.a1_pnl : null;
      const a1Th = (typeof sen.a1_threshold === "number") ? sen.a1_threshold : -500;
      const a1Trip = (a1 !== null) ? (a1 <= a1Th) : null;
      const a2 = (typeof sen.a2_velocity === "number") ? sen.a2_velocity : null;
      const a2Th = (typeof sen.a2_threshold === "number") ? sen.a2_threshold : -0.01;
      const a2Trip = (a2 !== null) ? (a2 <= a2Th) : null;
      const bDelta = (typeof sen.b_delta === "number") ? sen.b_delta : null;
      const bTrip = (bDelta !== null)
        ? ((r.side === "LONG") ? (bDelta < 0) : (bDelta > 0))
        : null;
      const stage = (typeof tg.stage === "number") ? tg.stage : null;
      const anchor = (typeof tg.anchor === "number") ? tg.anchor : null;
      const next = (typeof tg.next_target === "number") ? tg.next_target : null;
      const ratchet = (typeof tg.ratchet_steps === "number") ? tg.ratchet_steps : null;
      const stageLbl = (stage === null) ? "\u2014"
        : ("Stage " + stage);
      return '<div class="ts-row ts-row-multi">'
        + '<span class="ts-row-label">'
        +   '<span class="ticker">' + escapeHtml(r.ticker || "") + '</span> '
        +   '<span class="ts-side ts-side-' + (r.side === "SHORT" ? "short" : "long") + '">'
        +     escapeHtml(r.side || "\u2014") + '</span>'
        + '</span>'
        + '<span class="ts-row-value">'
        +   '<span class="ts-row-pair" title="Alarm A1 \u2014 hard P&L stop at -$500">'
        +     _tsCheck(a1Trip === null ? null : !a1Trip)
        +     ' A1 ' + escapeHtml(_tsMoney(a1)) + ' / ' + escapeHtml(_tsMoney(a1Th)) + '</span>'
        +   '<span class="ts-row-pair" title="Alarm A2 \u2014 velocity stop">'
        +     _tsCheck(a2Trip === null ? null : !a2Trip)
        +     ' A2 ' + escapeHtml(_tsNum(a2, 4)) + '/s</span>'
        +   '<span class="ts-row-pair" title="Alarm B \u2014 QQQ 5m close vs 9-EMA">'
        +     _tsCheck(bTrip === null ? null : !bTrip)
        +     ' B \u0394=' + escapeHtml(_tsNum(bDelta)) + '</span>'
        +   '<span class="ts-titan-stage">' + escapeHtml(stageLbl) + '</span>'
        +   '<span class="ts-row-pair mono">anchor=' + escapeHtml(_tsNum(anchor)) + '</span>'
        +   '<span class="ts-row-pair mono">next=' + escapeHtml(_tsNum(next)) + '</span>'
        +   '<span class="ts-row-pair mono">ratchet=' + (ratchet === null ? "\u2014" : ratchet) + '</span>'
        + '</span>'
        + '</div>';
    }).join("");
    body.innerHTML = html;
  }
  function renderTigerSovereign(s) {
    const ts = (s && s.tiger_sovereign) || null;
    const chip = $("ts-overall-chip");
    if (!ts) {
      if (chip) chip.textContent = "\u2014";
      return;
    }
    const p1 = ts.phase1 || {};
    const longPermit = _tsRenderPhase1Side("ts-phase1-long-body", "LONG", p1.long || {});
    const shortPermit = _tsRenderPhase1Side("ts-phase1-short-body", "SHORT", p1.short || {});
    _tsRenderPhase2(ts.phase2 || []);
    _tsRenderPhase3(ts.phase3 || []);
    _tsRenderPhase4(ts.phase4 || []);
    if (chip) {
      let label = "NO permit";
      if (longPermit && shortPermit) label = "BOTH-side permit";
      else if (longPermit) label = "LONG-only permit";
      else if (shortPermit) label = "SHORT-only permit";
      chip.textContent = label;
    }
  }

  // v5.13.2 \u2014 feature-flag pill renderer. ON = grey neutral
  // (spec-strict baseline). OFF = accent (operator override active).
  function renderFeatureFlags(state) {
    const ff = (state && state.feature_flags) || {};
    function setFlag(id, label, on) {
      const el = $(id);
      if (!el) return;
      el.classList.remove("ts-flag-on", "ts-flag-off");
      el.classList.add(on ? "ts-flag-on" : "ts-flag-off");
      el.textContent = label + ": " + (on ? "ON" : "OFF");
    }
    setFlag("ts-flag-vol", "Volume Gate", !!ff.volume_gate_enabled);
    setFlag("ts-flag-legacy", "Legacy Exits", !!ff.legacy_exits_enabled);
  }

  function renderGates(s) {
    const g = s.gates || {};
    const gates = $("gates-body");
    const rows = [];
    rows.push(gateRow("Trading halted", g.trading_halted ? "HALTED" : "Armed", !g.trading_halted, g.halt_reason || ""));
    rows.push(gateRow("Scan loop", g.scan_paused ? "Paused" : "Running", !g.scan_paused, ""));
    rows.push(gateRow("OR collected", g.or_collected_date || "—", !!g.or_collected_date, g.or_collected_date ? "" : "pending"));

    // v5.13.4 \u2014 per-ticker chip set surfaces Tiger Sovereign Phase 2
    // state: Permit (Phase 1 per-side QQQ permit), Vol (Volume Bucket
    // gate), Boundary (Boundary Hold 2-close confirmation). Phase 3
    // entry-fired status (E1/E2) is appended when present. Legacy
    // Brk/PDC/Idx/DI chips are no longer rendered \u2014 the spec rules
    // changed in v5.13.x and per-ticker PDC is exit-only behind a
    // runtime flag.
    const ts = (s && s.tiger_sovereign) || {};
    const phase2List = Array.isArray(ts.phase2) ? ts.phase2 : [];
    if (phase2List.length) {
      const phase1 = ts.phase1 || {};
      const longPermit = !!(phase1.long && phase1.long.permit);
      const shortPermit = !!(phase1.short && phase1.short.permit);
      const phase3List = Array.isArray(ts.phase3) ? ts.phase3 : [];
      const phase3ByTicker = {};
      for (const r of phase3List) {
        if (!r || !r.ticker) continue;
        if (!phase3ByTicker[r.ticker]) phase3ByTicker[r.ticker] = [];
        phase3ByTicker[r.ticker].push(r);
      }
      const ff = (s && s.feature_flags) || {};
      const volOff = !ff.volume_gate_enabled;
      const notes = [];
      if (volOff) notes.push("Volume Gate runtime override: OFF");
      const sub = notes.length ? ` <span class="hint">\u2014 ${escapeHtml(notes.join(" \u00b7 "))}</span>` : "";
      rows.push(`<div class="tgate-section-label">Per-ticker \u00b7 Permit \u00b7 Vol \u00b7 Boundary${sub}</div>`);
      for (const r of phase2List) {
        rows.push(tGateRow(r, longPermit, shortPermit, phase3ByTicker[r.ticker] || []));
      }
    }
    gates.innerHTML = rows.join("");

    // v3.4.21 — next-scan countdown visible in header tick.
    const nss = (typeof g.next_scan_sec === "number") ? g.next_scan_sec : null;
    window.__nextScanSec = nss;
    updateNextScanLabel();
  }

  function tGateRow(r, longPermit, shortPermit, phase3Rows) {
    if (!r || !r.ticker) return "";
    const chips = [];
    chips.push(permitChip(longPermit, shortPermit));
    chips.push(volChip(r.vol_gate_status));
    chips.push(boundaryChip(r.two_consec_above, r.two_consec_below));
    const fired = entryFiredChips(phase3Rows || []);
    if (fired) chips.push(fired);
    return `<div class="tgate">
      <span class="tgate-tkr">${escapeHtml(r.ticker)}</span>
      <span class="tgate-chips">${chips.join("")}</span>
    </div>`;
  }

  function permitChip(longPermit, shortPermit) {
    const lCls = longPermit ? "on" : "off";
    const sCls = shortPermit ? "on" : "off";
    const lTxt = longPermit ? "L\u2713" : "L\u2717";
    const sTxt = shortPermit ? "S\u2713" : "S\u2717";
    const title = "Phase 1 \u2014 Section I QQQ permit (per side)";
    return `<span class="tgate-chip ${lCls}" title="${escapeHtml(title)}">${escapeHtml(lTxt)}</span>` +
           `<span class="tgate-chip ${sCls}" title="${escapeHtml(title)}">${escapeHtml(sTxt)}</span>`;
  }

  function volChip(status) {
    const s = (status || "").toString().toUpperCase();
    let cls = "na";
    if (s === "PASS") cls = "on";
    else if (s === "FAIL") cls = "off";
    else if (s === "COLD") cls = "warm";
    else if (s === "OFF") cls = "na";
    const lbl = s ? `Vol ${s}` : "Vol \u2014";
    const title = s === "OFF"
      ? "Volume Bucket gate \u2014 VOLUME_GATE_ENABLED override is OFF"
      : "Volume Bucket gate";
    return `<span class="tgate-chip ${cls}" title="${escapeHtml(title)}">${escapeHtml(lbl)}</span>`;
  }

  function boundaryChip(twoAbove, twoBelow) {
    const title = "Boundary Hold (2 consecutive closes)";
    if (twoAbove) {
      return `<span class="tgate-chip on" title="${escapeHtml(title)}">\u2191\u2191</span>`;
    }
    if (twoBelow) {
      return `<span class="tgate-chip on" title="${escapeHtml(title)}">\u2193\u2193</span>`;
    }
    return `<span class="tgate-chip na" title="${escapeHtml(title)}">\u2026</span>`;
  }

  function entryFiredChips(phase3Rows) {
    let e1 = false, e2 = false;
    for (const r of phase3Rows) {
      if (r.entry1_fired) e1 = true;
      if (r.entry2_fired) e2 = true;
    }
    if (!e1 && !e2) return "";
    const out = [];
    if (e1) out.push(`<span class="tgate-chip on" title="Entry 1 fired">E1\u2713</span>`);
    if (e2) out.push(`<span class="tgate-chip on" title="Entry 2 fired">E2\u2713</span>`);
    return out.join("");
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

  function gateRow(name, value, ok, hint) {
    const chipCls = ok ? "chip-ok" : "chip-down";
    const chipTxt = ok ? "Pass" : "Check";
    return `<div class="gate">
      <span class="gate-name">${escapeHtml(name)}${hint ? ` <span class="hint">${escapeHtml(hint)}</span>` : ""}</span>
      <span class="mono">${escapeHtml(value)}</span>
      <span class="chip ${chipCls}">${chipTxt}</span>
    </div>`;
  }

  function renderHeader(s) {
    const ver = `v${s.version || "?"}`;
    const verEl = document.getElementById("tg-brand-ver");
    if (verEl) verEl.textContent = ver;
    // v4.2.2 \u2014 extract tz token (ET/CDT/CT/PT/PST/\u2026) from
    // server_time_label tail, e.g. "Fri Apr 24 \u00b7 13:09:13 ET".
    // The client-side tick loop renders the actual HH:MM:SS every
    // second; we only cache the tz label here so the clock shows it.
    const lbl = s.server_time_label || "";
    const m = lbl.match(/\d{1,2}:\d{2}:\d{2}\s+([A-Z]{2,4})\s*$/);
    if (m) window.__tgClockTz = m[1];
    if (typeof window.__tgTickClock === "function") window.__tgTickClock();
  }

  // v5.5.7 \u2014 Main tab LAST SIGNAL card. Mirrors the exec-panel
  // formatting (kind / ticker / price / reason / timestamp). Reads
  // s.last_signal which is the paper executor's most recent emitted
  // signal (entry/exit/eod). Empty/null \u2192 "No signals received yet."
  function renderLastSignal(s) {
    const ls = s && s.last_signal;
    const chip = $("last-sig-chip");
    const body = $("last-sig-body");
    if (chip) chip.textContent = (ls && ls.kind) ? ls.kind : "none";
    if (!body) return;
    if (!ls || !ls.kind) {
      body.innerHTML = `<div class="empty">No signals received yet.</div>`;
      return;
    }
    const px = (typeof ls.price === "number" && ls.price)
      ? " @ " + fmtUsd(ls.price) : "";
    const reason = ls.reason ? ` \u00b7 ${escapeHtml(ls.reason)}` : "";
    body.innerHTML = `<div class="mono" style="font-size:12px;color:var(--text-muted)">
      <span style="color:var(--accent)">${escapeHtml(ls.kind)}</span>
      ${escapeHtml(ls.ticker || "")}${px}${reason}
      <span style="color:var(--text-dim)"> \u00b7 ${escapeHtml(ls.timestamp_utc || "")}</span>
    </div>`;
  }

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
    renderProximity(s);
    renderTrades(s, sl);
    renderLastSignal(s);
    renderObserver(s);
    renderGates(s);
    try { renderTigerSovereign(s); } catch (e) { /* never break Main */ }
    try { renderFeatureFlags(s); } catch (e) { /* never break Main */ }
    // v5.2.0 — Shadow strategy P&L panel (bottom of main dashboard).
    try { renderShadowPnL(s); } catch (e) {}
    // v4.11.0 — health pill bound to Main when active.
    try { applyHealthPill("main", s.errors || { count: 0, severity: "green", entries: [] }); } catch (e) {}
  }

  // ─────── shadow strategy P&L panel (v5.2.0) ───────
  function _fmtShadowMoney(v) {
    if (v === null || v === undefined) return "--";
    const n = Number(v);
    if (!isFinite(n)) return "--";
    const sign = n >= 0 ? "+" : "-";
    const abs = Math.abs(n);
    return sign + "$" + abs.toFixed(2);
  }
  function _fmtShadowWR(wr, n) {
    if (n === 0 || wr === null || wr === undefined) return "--";
    return "WR=" + Number(wr).toFixed(1) + "%";
  }
  function _shadowSectionHTML(stats) {
    const n = Number(stats.n || 0);
    const total = Number(stats.total || 0);
    const realized = Number(stats.realized || 0);
    const unreal = Number(stats.unrealized || 0);
    const wrTxt = _fmtShadowWR(stats.wr, n);
    const totalCls = total > 0 ? "sp-pnl-pos" : (total < 0 ? "sp-pnl-neg" : "");
    let unrealTxt = "";
    if (Math.abs(unreal) >= 0.005 && n > 0) {
      unrealTxt = ' <span class="sp-meta">(' + _fmtShadowMoney(unreal) + ' unr)</span>';
    }
    return (
      '<span class="sp-meta">n=' + n + '</span>' +
      '<span class="sp-meta">' + wrTxt + '</span>' +
      '<span class="' + totalCls + '">' + (n > 0 ? _fmtShadowMoney(total) : "--") + '</span>' +
      unrealTxt
    );
  }
  // v5.3.0 \u2014 detail-view formatters / table builders.
  function _fmtShadowETHHMM(tsUtc) {
    if (!tsUtc) return "\u2014";
    try {
      const d = new Date(tsUtc);
      if (isNaN(d.getTime())) return "\u2014";
      // Render in America/New_York. Falls back to UTC HH:MM if Intl
      // tz support is missing on the host browser.
      try {
        return d.toLocaleTimeString("en-US", {
          hour12: false, hour: "2-digit", minute: "2-digit",
          timeZone: "America/New_York",
        });
      } catch (e) {
        const hh = String(d.getUTCHours()).padStart(2, "0");
        const mm = String(d.getUTCMinutes()).padStart(2, "0");
        return hh + ":" + mm;
      }
    } catch (e) { return "\u2014"; }
  }
  function _fmtShadowPrice(v) {
    if (v === null || v === undefined) return "\u2014";
    const n = Number(v);
    if (!isFinite(n)) return "\u2014";
    return "$" + n.toFixed(2);
  }
  function _fmtShadowPct(num, denom) {
    const n = Number(num);
    const d = Number(denom);
    if (!isFinite(n) || !isFinite(d) || d === 0) return "\u2014";
    const p = (n / d) * 100;
    const sign = p >= 0 ? "+" : "\u2212";
    return sign + Math.abs(p).toFixed(2) + "%";
  }
  function _shadowSideClass(side) {
    return String(side || "").toLowerCase().indexOf("short") >= 0
      ? "sd-side-short" : "sd-side-long";
  }
  function _shadowSideLabel(side) {
    const s = String(side || "").toLowerCase();
    return s === "short" ? "SHORT" : "LONG";
  }
  function _shadowOpenTable(rows) {
    if (!rows || !rows.length) {
      return '<div class="sp-detail-empty">No open positions.</div>';
    }
    const head = '<thead><tr>'
      + '<th>Ticker</th><th>Side</th><th>Qty</th>'
      + '<th>Entry</th><th>Mark</th><th>P&amp;L $</th>'
      + '<th>P&amp;L %</th><th>Open</th>'
      + '</tr></thead>';
    const body = rows.map(r => {
      const entry = Number(r.entry_price || 0);
      const mark = (r.current_mark === null || r.current_mark === undefined)
        ? null : Number(r.current_mark);
      const unr = Number(r.unrealized || 0);
      const denom = Math.abs(entry * Number(r.qty || 0));
      const pct = denom > 0 ? (unr / denom) * 100 : null;
      const pnlCls = unr > 0 ? "sd-pos" : (unr < 0 ? "sd-neg" : "");
      const pctTxt = pct === null ? "\u2014"
        : (pct >= 0 ? "+" : "\u2212") + Math.abs(pct).toFixed(2) + "%";
      return '<tr>'
        + '<td>' + escapeHtml(r.ticker || "") + '</td>'
        + '<td class="' + _shadowSideClass(r.side) + '">'
          + _shadowSideLabel(r.side) + '</td>'
        + '<td>' + Number(r.qty || 0) + '</td>'
        + '<td>' + _fmtShadowPrice(entry) + '</td>'
        + '<td>' + (mark === null ? "\u2014" : _fmtShadowPrice(mark)) + '</td>'
        + '<td class="' + pnlCls + '">' + _fmtShadowMoney(unr) + '</td>'
        + '<td class="' + pnlCls + '">' + pctTxt + '</td>'
        + '<td>' + _fmtShadowETHHMM(r.entry_ts_utc) + '</td>'
        + '</tr>';
    }).join("");
    return '<table class="sp-detail-table">' + head
      + '<tbody>' + body + '</tbody></table>';
  }
  function _shadowClosedTable(rows) {
    if (!rows || !rows.length) {
      return '<div class="sp-detail-empty">No recent closed trades.</div>';
    }
    const head = '<thead><tr>'
      + '<th>Ticker</th><th>Side</th><th>Qty</th>'
      + '<th>Entry</th><th>Exit</th><th>P&amp;L $</th>'
      + '<th>P&amp;L %</th><th>Reason</th><th>Exit</th>'
      + '</tr></thead>';
    const body = rows.map(r => {
      const entry = Number(r.entry_price || 0);
      const exit_ = Number(r.exit_price || 0);
      const pnl = Number(r.realized_pnl || 0);
      const denom = Math.abs(entry * Number(r.qty || 0));
      const pct = denom > 0 ? (pnl / denom) * 100 : null;
      const pnlCls = pnl > 0 ? "sd-pos" : (pnl < 0 ? "sd-neg" : "");
      const pctTxt = pct === null ? "\u2014"
        : (pct >= 0 ? "+" : "\u2212") + Math.abs(pct).toFixed(2) + "%";
      return '<tr>'
        + '<td>' + escapeHtml(r.ticker || "") + '</td>'
        + '<td class="' + _shadowSideClass(r.side) + '">'
          + _shadowSideLabel(r.side) + '</td>'
        + '<td>' + Number(r.qty || 0) + '</td>'
        + '<td>' + _fmtShadowPrice(entry) + '</td>'
        + '<td>' + _fmtShadowPrice(exit_) + '</td>'
        + '<td class="' + pnlCls + '">' + _fmtShadowMoney(pnl) + '</td>'
        + '<td class="' + pnlCls + '">' + pctTxt + '</td>'
        + '<td>' + escapeHtml(r.exit_reason || "") + '</td>'
        + '<td>' + _fmtShadowETHHMM(r.exit_ts_utc) + '</td>'
        + '</tr>';
    }).join("");
    return '<table class="sp-detail-table">' + head
      + '<tbody>' + body + '</tbody></table>';
  }
  function _shadowDetailHTML(cfg) {
    return (
      '<div class="sp-detail">'
        + '<div class="sp-detail-head">Open positions ('
          + ((cfg.open_positions || []).length) + ')</div>'
        + _shadowOpenTable(cfg.open_positions || [])
        + '<div class="sp-detail-head">Recent closed trades</div>'
        + _shadowClosedTable(cfg.recent_trades || [])
      + '</div>'
    );
  }
  // Track which config rows are currently expanded so re-renders on
  // state polls do not collapse what the user opened.
  const __shadowExpanded = new Set();

  // v5.5.9 \u2014 top summary band on the Shadow tab. Aggregates
  // per-config open_positions into a single strip showing total open,
  // total unrealized $, the most-active config, and the state's
  // "as_of" timestamp. Mirrors the index ticker strip pattern.
  function _shadowSummaryBand(s) {
    const band = document.getElementById("shadow-summary-band");
    if (!band) return;
    const sp = s && s.shadow_pnl;
    const cfgs = (sp && Array.isArray(sp.configs)) ? sp.configs : [];
    let totalOpen = 0;
    let totalUnreal = 0;
    let mostActive = null;
    let mostActiveN = 0;
    for (const cfg of cfgs) {
      const opens = Array.isArray(cfg.open_positions) ? cfg.open_positions : [];
      totalOpen += opens.length;
      for (const p of opens) {
        const u = Number(p && p.unrealized);
        if (isFinite(u)) totalUnreal += u;
      }
      if (opens.length > mostActiveN) {
        mostActiveN = opens.length;
        mostActive = cfg.name || null;
      }
    }
    const openEl = document.getElementById("ssb-open");
    const unrEl = document.getElementById("ssb-unr");
    const actEl = document.getElementById("ssb-active");
    const asofEl = document.getElementById("ssb-asof");
    if (openEl) openEl.textContent = String(totalOpen);
    if (unrEl) {
      unrEl.textContent = totalOpen > 0 ? _fmtShadowMoney(totalUnreal) : "\u2014";
      unrEl.classList.remove("up", "down");
      if (totalOpen > 0) {
        if (totalUnreal > 0) unrEl.classList.add("up");
        else if (totalUnreal < 0) unrEl.classList.add("down");
      }
    }
    if (actEl) {
      actEl.textContent = mostActive
        ? mostActive + " \u00b7 " + mostActiveN
        : "\u2014";
    }
    if (asofEl) {
      // v5.5.10 \u2014 /api/state has no top-level as_of field; the
      // canonical timestamp is server_time, with shadow_pnl.as_of as
      // a fallback. The pre-fix s.as_of read was always undefined.
      // v5.5.11 hotfix \u2014 _scFmtTs lives in a sibling IIFE and is
      // not reachable from here; calling it threw ReferenceError and
      // the wrapping try/catch in renderShadowPnL swallowed it, so
      // this cell stayed on the placeholder. Inline a self-contained
      // formatter to keep the fix local to IIFE-1.
      const asof = (s && (s.server_time || (s.shadow_pnl && s.shadow_pnl.as_of))) || null;
      let formatted = "\u2014";
      if (asof) {
        try {
          const d = new Date(asof);
          if (!isNaN(d.getTime())) {
            const opts = {
              timeZone: "America/New_York", month: "2-digit", day: "2-digit",
              hour: "2-digit", minute: "2-digit", hour12: false,
            };
            const parts = new Intl.DateTimeFormat("en-US", opts)
              .formatToParts(d).reduce((a, p) => (a[p.type] = p.value, a), {});
            formatted = parts.month + "/" + parts.day + " "
                      + parts.hour + ":" + parts.minute + " ET";
          } else {
            formatted = String(asof);
          }
        } catch (e) {
          formatted = String(asof);
        }
      }
      asofEl.textContent = formatted;
    }
  }

  // v5.13.2 \u2014 the v5.10.6 Eye-of-the-Tiger renderer was retired in
  // favor of renderTigerSovereign() above. The legacy /api/state
  // fields (section_i_permit, per_ticker_v510, per_position_v510)
  // remain for backward compat but are no longer drawn here.


  function renderShadowPnL(s) {
    // v5.5.9 \u2014 keep the top summary band in sync with the same
    // payload that drives the strategies table.
    try { _shadowSummaryBand(s); } catch (e) {}
    // v5.5.9 \u2014 the per-config unrealized bar-chart fallback reads
    // open_positions from this exact payload, so re-render charts on
    // every state tick (matches the 5s state poll rather than the 60s
    // /api/shadow_charts cadence). Cheap when no fallback bars apply.
    try {
      if (typeof window.__tgPollShadowCharts === "function" && __scLastPayload) {
        _scRender(__scLastPayload);
      }
    } catch (e) {}
    const sp = s && s.shadow_pnl;
    const body = $("shadow-pnl-body");
    if (!body) return;
    const cnt = $("shadow-count");
    const bestChip = $("shadow-best");
    // v5.5.3 \u2014 SHADOW DISABLED pill on the shadow card head when
    // the bot couldn't bind Alpaca market-data credentials at boot.
    const statusPill = $("shadow-status-pill");
    if (statusPill) {
      const status = s && s.shadow_data_status;
      statusPill.style.display = (status === "disabled_no_creds") ? "" : "none";
    }
    if (!sp || !sp.configs) {
      body.innerHTML = '<div class="empty">Waiting for shadow data\u2026</div>';
      if (cnt) cnt.textContent = "\u00b7 \u2014";
      if (bestChip) bestChip.textContent = "\u2014";
      return;
    }
    const rows = [];
    let activeCount = 0;
    for (const cfg of sp.configs) {
      const cls = ["shadow-pnl-row", "sp-expandable"];
      if (sp.best_today && cfg.name === sp.best_today) cls.push("sp-best");
      if (sp.worst_today && cfg.name === sp.worst_today) cls.push("sp-worst");
      // v5.5.9 B3 \u2014 subtle row tint by today's P&L sign. Skipped
      // when the row already paints sp-best / sp-worst (those have a
      // saturated background that would clash with the tint). Uses
      // today.total (realized + unrealized) so configs with only open
      // positions still get a visual sentiment cue.
      const todayPnl = Number((cfg.today || {}).total);
      const todayNRow = Number((cfg.today || {}).n || 0);
      const todayHasOpens = Array.isArray(cfg.open_positions)
        && cfg.open_positions.length > 0;
      const isHighlighted = cls.includes("sp-best") || cls.includes("sp-worst");
      if (!isHighlighted && isFinite(todayPnl)
          && (todayNRow > 0 || todayHasOpens)) {
        if (todayPnl > 0) cls.push("sp-tint-pos");
        else if (todayPnl < 0) cls.push("sp-tint-neg");
      }
      const todayN = Number((cfg.today || {}).n || 0);
      if (todayN > 0) activeCount += 1;
      const expanded = __shadowExpanded.has(cfg.name);
      const safeName = escapeHtml(cfg.name);
      rows.push(
        '<div class="' + cls.join(" ") + '" data-sp-config="' + safeName + '"'
          + ' role="button" tabindex="0"'
          + ' aria-expanded="' + (expanded ? "true" : "false") + '">' +
          '<span class="sp-name"><span class="sp-chev">' + (expanded ? "\u25be" : "\u25b8") + '</span>' + cfg.label + '</span>' +
          '<span class="sp-section sp-today">' + _shadowSectionHTML(cfg.today || {}) + '</span>' +
          '<span class="sp-section sp-cum">' + _shadowSectionHTML(cfg.cumulative || {}) + '</span>' +
        '</div>'
      );
      if (expanded) {
        rows.push(_shadowDetailHTML(cfg));
      }
    }
    // v5.2.0 amendment \u2014 the comparator row is now the PAPER BOT
    // (same portfolio whose equity drives shadow sizing). Older
    // backend snapshots that still ship `live_bot` are accepted as a
    // fallback so a stale browser tab doesn't blank the row during
    // rollout.
    const cmp = sp.paper_bot || sp.live_bot;
    if (cmp) {
      rows.push(
        '<div class="shadow-pnl-row sp-live">' +
          '<span class="sp-name">' + cmp.label + '</span>' +
          '<span class="sp-section sp-today">' + _shadowSectionHTML(cmp.today || {}) + '</span>' +
          '<span class="sp-section sp-cum">' + _shadowSectionHTML(cmp.cumulative || {}) + '</span>' +
        '</div>'
      );
    }
    body.innerHTML = rows.join("");
    if (cnt) cnt.textContent = "\u00b7 " + sp.configs.length + " configs";
    if (bestChip) {
      bestChip.textContent = sp.best_today ? ("best: " + sp.best_today) : "\u2014";
    }
  }

  // v5.3.0 \u2014 row click toggles per-config detail. Event delegation
  // on the body so the handler survives every re-render.
  (function __wireShadowToggle() {
    const body = document.getElementById("shadow-pnl-body");
    if (!body) return;
    function toggleFromTarget(target) {
      const row = target.closest && target.closest(".shadow-pnl-row.sp-expandable");
      if (!row) return;
      const name = row.getAttribute("data-sp-config");
      if (!name) return;
      if (__shadowExpanded.has(name)) __shadowExpanded.delete(name);
      else __shadowExpanded.add(name);
      const s = window.__tgLastState;
      if (s) renderShadowPnL(s);
    }
    body.addEventListener("click", (e) => toggleFromTarget(e.target));
    body.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" && e.key !== " ") return;
      e.preventDefault();
      toggleFromTarget(e.target);
    });
  })();

  // v4.1.8-dash \u2014 portfolio view toggle removed (Robinhood was
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
      // reconnects \u2014 the 3s stale-data watchdog can fire back-to-back
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

  // v4.2.2 \u2014 client-side 1Hz clock tick. Renders HH:MM:SS + tz
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
    // v4.3.1 \u2014 drop seconds on very narrow phones (<=360px) so
    // the HH:MM TZ label fits inline with logo/version/LIVE pill.
    const narrow = window.matchMedia && window.matchMedia("(max-width: 360px)").matches;
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
})();

(() => {
  // v4.0.0-beta — tab switcher, index strip, and per-executor polling.
  // All vanilla JS. Independent from the main-tab IIFE above so nothing
  // the main tab does can interfere.
  //
  // v4.10.2 — the GATE tri-state helper lives in the *first* IIFE.
  // Alias the window-bridged copy to a local name so the rest of this
  // IIFE can call it without changing every call site. If the bridge
  // is missing for any reason, fall back to a no-op so a missing
  // helper can never throw and surface as "Fetch failed: …" again.
  const applyGateTriState = (typeof window !== "undefined" && typeof window.__tgApplyGateTriState === "function")
    ? window.__tgApplyGateTriState
    : function () { /* no-op fallback */ };
  // v4.11.0 — health-pill helper bridge, same pattern as the gate one.
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
  const TABS = ["main", "val", "gene", "shadow", "lifecycle"];
  let activeTab = "main";

  // v4.1.4-dash \u2014 H2: one-shot /api/state warmup when user lands on
  // Val/Gene before Main has populated window.__tgLastState. Without
  // this, shared KPIs (Gate/Regime/Session) render as "\u2014" for up to
  // 15s until the executor poll + Main SSE tick both land. Guarded so
  // we only fire once per tab-switch event, and never blocks the
  // executor poll \u2014 both run in parallel.
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
      // swallow \u2014 next Main SSE/poll tick will populate
    } finally {
      __tgWarmupInFlight = false;
    }
  }

  function selectTab(name) {
    if (!TABS.includes(name)) return;
    activeTab = name;
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
    if (name === "shadow") {
      // Shadow tab is fed by /api/state shadow_pnl block (same source
      // as Main). Warm up once on first visit so the panel paints
      // without waiting for the Main SSE/poll tick.
      if (!window.__tgLastState) {
        warmupSharedState();
      } else if (typeof renderShadowPnL === "function") {
        renderShadowPnL(window.__tgLastState);
      }
      // v5.4.1 \u2014 charts are tab-scoped: poll /api/shadow_charts only
      // while the Shadow tab is active. Triggers an immediate fetch on
      // tab activation; the 60s loop below skips ticks on other tabs.
      if (typeof window.__tgPollShadowCharts === "function") {
        window.__tgPollShadowCharts();
      }
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

    <section class="kpi-row">
      <div class="kpi"><span class="kpi-label">Equity</span><span class="kpi-value" data-f="k-equity">\u2014</span><span class="kpi-sub" data-f="k-equity-sub">\u2014</span></div>
      <div class="kpi"><span class="kpi-label">Day P&amp;L</span><span class="kpi-value" data-f="k-pnl">\u2014</span><span class="kpi-sub" data-f="k-pnl-sub">\u2014</span></div>
      <div class="kpi"><span class="kpi-label">Open</span><span class="kpi-value" data-f="k-open">\u2014</span><span class="kpi-sub" data-f="k-open-sub">\u2014</span></div>
      <div class="kpi"><span class="kpi-label">Gate</span><span class="kpi-value" data-f="k-gate" style="font-size:20px">\u2014</span><span class="kpi-sub" data-f="k-gate-sub">\u2014</span></div>
      <div class="kpi"><span class="kpi-label">Regime</span><span class="kpi-value" data-f="k-regime" style="font-size:20px">\u2014</span><span class="kpi-sub" data-f="k-regime-sub">\u2014</span></div>
      <div class="kpi"><span class="kpi-label">Session</span><span class="kpi-value" data-f="k-session" style="font-size:20px">\u2014</span><span class="kpi-sub" data-f="k-session-sub">\u2014</span></div>
    </section>

    <section class="grid grid-2">
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

      <div class="card">
        <div class="card-head"><span class="card-title">Proximity<span class="count" data-f="prox-count">\u00b7 \u2014</span></span></div>
        <div class="card-body flush" data-f="prox-list">
          <div class="empty">Loading\u2026</div>
        </div>
      </div>
    </section>

    <section class="grid grid-2">
      <div class="card">
        <div class="card-head">
          <span class="card-title">Last signal</span>
          <span class="chip" data-f="last-sig-chip">\u2014</span>
        </div>
        <div class="card-body" data-f="last-sig-body">
          <div class="empty">No signals received yet.</div>
        </div>
      </div>
      <div class="card">
        <div class="card-head"><span class="card-title">Today's trades<span class="count" data-f="trades-count">\u00b7 \u2014</span></span><span class="chip" data-f="trades-realized">\u2014</span></div>
        <div class="card-body flush" data-f="trades-body">
          <div class="empty">No trades today.</div>
        </div>
      </div>
    </section>

    <section class="grid grid-2">
      <div class="card">
        <div class="card-head">
          <span class="card-title">Sovereign Regime Shield</span>
          <span class="chip" data-f="srs-status">\u2014</span>
        </div>
        <div class="card-body" data-f="srs-body">
          <div class="empty">Loading\u2026</div>
        </div>
      </div>
      <div class="card">
        <div class="card-head"><span class="card-title">Gates \u00b7 entry checks</span></div>
        <div data-f="gates-body">
          <div class="empty">Loading\u2026</div>
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
    const el = $$("tg-badge-" + name);
    if (!el) return;
    if (!data || data.enabled === false) {
      el.textContent = "off";
      el.style.color = "#5b6572";
      return;
    }
    if (data.mode === "live") {
      el.textContent = "\ud83d\udfe2 Live";
      el.style.color = "#34d399";
    } else {
      el.textContent = "\ud83d\udcc4 Paper";
      el.style.color = "#8a96a7";
    }
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

  // --- Market-state widgets (proximity, SRS, gates) ------------------
  // These are scanner-level signals that are the same for every
  // executor by design, so we render them from the Main /api/state
  // payload (republished on window.__tgLastState) rather than from the
  // per-executor snapshot. Logic mirrors Main's renderProximity /
  // renderSovereign / renderGates.

  function fmtPxExec(v) {
    if (v === null || v === undefined || isNaN(v)) return "\u2014";
    return "$" + Number(v).toFixed(2);
  }

  function renderExecProximity(panel, s) {
    const list = execField(panel, "prox-list");
    const count = execField(panel, "prox-count");
    if (!list) return;
    const rows = (s && Array.isArray(s.proximity)) ? s.proximity : [];
    if (count) count.textContent = "\u00b7 " + rows.length + " tracked";
    if (!rows.length) {
      list.innerHTML = `<div class="empty">No tickers configured.</div>`;
      return;
    }
    const html = rows.map((r) => {
      const pct = (r.nearest_pct !== null && r.nearest_pct !== undefined) ? r.nearest_pct : null;
      let fill = 0;
      if (pct !== null) fill = Math.max(0, Math.min(100, Math.round((1 - Math.min(pct, 0.02) / 0.02) * 100)));
      const warn = pct !== null && pct < 0.005;
      const mark = r.open_side === "SHORT"
        ? '<span class="mark mark-short" title="Open short">\u25CF</span>'
        : r.open_side === "LONG"
          ? '<span class="mark mark-long" title="Open long">\u25CF</span>'
          : "";
      const lbl = r.nearest_label || "\u2014";
      const pctText = pct !== null ? `${(pct * 100).toFixed(2)}% \u00b7 ${lbl}` : "\u2014";
      const permitSide = r.permit_side || "NONE";
      const permitChip = execRenderPermitSideChip(permitSide);
      const dim = (permitSide === "NONE") ? ' style="opacity:0.55"' : '';
      return `<div class="prox-row"${dim}>
        <span class="prox-ticker">${esc(r.ticker)} ${mark}</span>
        <span class="prox-price">${fmtPxExec(r.price)}</span>
        <div class="prox-bar"><div class="prox-fill ${warn ? "warn" : "ok"}" style="width:${fill}%"></div></div>
        <span class="prox-pct" style="color:${warn ? 'var(--warn)' : 'var(--text-muted)'}">${esc(pctText)} ${permitChip}</span>
      </div>`;
    }).join("");
    list.innerHTML = html;
  }

  function execRenderPermitSideChip(permitSide) {
    if (permitSide === "BOTH") return `<span class="tgate-chip on" title="Phase 1 permit: long + short">L+S</span>`;
    if (permitSide === "LONG") return `<span class="tgate-chip on" title="Phase 1 long permit active">L</span>`;
    if (permitSide === "SHORT") return `<span class="tgate-chip on" title="Phase 1 short permit active">S</span>`;
    return `<span class="tgate-chip na" title="No Phase 1 permit \u2014 breakout would not fire">no permit</span>`;
  }

  function renderExecSovereign(panel, s) {
    const body = execField(panel, "srs-body");
    const chip = execField(panel, "srs-status");
    if (!body || !chip) return;
    const srs = (s && s.regime && s.regime.sovereign) || null;
    if (!srs) {
      body.innerHTML = '<div class="empty">Regime data unavailable.</div>';
      chip.textContent = "\u2014";
      chip.className = "chip";
      return;
    }
    const status = srs.status || "NO_PDC";
    let chipCls = "chip chip-srs-dis";
    let chipTxt = "Disarmed";
    if (status === "ARMED_LONG")       { chipCls = "chip chip-srs-armed"; chipTxt = "Long-eject armed"; }
    else if (status === "ARMED_SHORT") { chipCls = "chip chip-srs-armed"; chipTxt = "Short-eject armed"; }
    else if (status === "AWAITING")    { chipCls = "chip chip-srs-wait";  chipTxt = "Awaiting 1m close"; }
    else if (status === "NO_PDC")      { chipCls = "chip chip-srs-wait";  chipTxt = "No PDC yet"; }
    chip.textContent = chipTxt;
    chip.className = chipCls;

    const idxRow = (sym, price, pdc, delta) => {
      const priceTxt = (typeof price === "number") ? price.toFixed(2) : "\u2014";
      const pdcTxt = (typeof pdc === "number") ? `PDC ${pdc.toFixed(2)}` : "PDC \u2014";
      let deltaCls = "srs-delta na";
      let deltaTxt = "\u2014";
      if (typeof delta === "number") {
        deltaCls = "srs-delta " + (delta >= 0 ? "up" : "down");
        const sign = delta >= 0 ? "+" : "";
        deltaTxt = `${sign}${delta.toFixed(2)}%`;
      }
      return `<div class="srs-idx">
        <span class="srs-sym">${esc(sym)}</span>
        <span class="srs-price">${esc(priceTxt)}</span>
        <span class="srs-pdc">${esc(pdcTxt)}</span>
        <span class="${deltaCls}">${esc(deltaTxt)}</span>
      </div>`;
    };
    // v5.9.1: PDC eject rule retired \u2014 eject tiles removed.
    body.innerHTML = `<div class="srs">
      ${idxRow("SPY", srs.spy_price, srs.spy_pdc, srs.spy_delta_pct)}
      ${idxRow("QQQ", srs.qqq_price, srs.qqq_pdc, srs.qqq_delta_pct)}
      <div class="srs-reason">${esc(srs.reason || "")}</div>
    </div>`;
  }

  function execPermitChip(longPermit, shortPermit) {
    const lCls = longPermit ? "on" : "off";
    const sCls = shortPermit ? "on" : "off";
    const lTxt = longPermit ? "L\u2713" : "L\u2717";
    const sTxt = shortPermit ? "S\u2713" : "S\u2717";
    const title = "Phase 1 \u2014 Section I QQQ permit (per side)";
    return `<span class="tgate-chip ${lCls}" title="${esc(title)}">${esc(lTxt)}</span>` +
           `<span class="tgate-chip ${sCls}" title="${esc(title)}">${esc(sTxt)}</span>`;
  }
  function execVolChip(status) {
    const s = (status || "").toString().toUpperCase();
    let cls = "na";
    if (s === "PASS") cls = "on";
    else if (s === "FAIL") cls = "off";
    else if (s === "COLD") cls = "warm";
    else if (s === "OFF") cls = "na";
    const lbl = s ? `Vol ${s}` : "Vol \u2014";
    const title = s === "OFF"
      ? "Volume Bucket gate \u2014 VOLUME_GATE_ENABLED override is OFF"
      : "Volume Bucket gate";
    return `<span class="tgate-chip ${cls}" title="${esc(title)}">${esc(lbl)}</span>`;
  }
  function execBoundaryChip(twoAbove, twoBelow) {
    const title = "Boundary Hold (2 consecutive closes)";
    if (twoAbove) {
      return `<span class="tgate-chip on" title="${esc(title)}">\u2191\u2191</span>`;
    }
    if (twoBelow) {
      return `<span class="tgate-chip on" title="${esc(title)}">\u2193\u2193</span>`;
    }
    return `<span class="tgate-chip na" title="${esc(title)}">\u2026</span>`;
  }
  function execEntryFiredChips(phase3Rows) {
    let e1 = false, e2 = false;
    for (const r of phase3Rows) {
      if (r.entry1_fired) e1 = true;
      if (r.entry2_fired) e2 = true;
    }
    if (!e1 && !e2) return "";
    const out = [];
    if (e1) out.push(`<span class="tgate-chip on" title="Entry 1 fired">E1\u2713</span>`);
    if (e2) out.push(`<span class="tgate-chip on" title="Entry 2 fired">E2\u2713</span>`);
    return out.join("");
  }
  function execTGateRow(r, longPermit, shortPermit, phase3Rows) {
    if (!r || !r.ticker) return "";
    const chips = [];
    chips.push(execPermitChip(longPermit, shortPermit));
    chips.push(execVolChip(r.vol_gate_status));
    chips.push(execBoundaryChip(r.two_consec_above, r.two_consec_below));
    const fired = execEntryFiredChips(phase3Rows || []);
    if (fired) chips.push(fired);
    return `<div class="tgate">
      <span class="tgate-tkr">${esc(r.ticker)}</span>
      <span class="tgate-chips">${chips.join("")}</span>
    </div>`;
  }
  function execGateRow(name, value, ok, hint) {
    const chipCls = ok ? "chip-ok" : "chip-down";
    const chipTxt = ok ? "Pass" : "Check";
    return `<div class="gate">
      <span class="gate-name">${esc(name)}${hint ? ` <span class="hint">${esc(hint)}</span>` : ""}</span>
      <span class="mono">${esc(value)}</span>
      <span class="chip ${chipCls}">${chipTxt}</span>
    </div>`;
  }
  function renderExecGates(panel, s) {
    const gates = execField(panel, "gates-body");
    if (!gates) return;
    const g = (s && s.gates) || {};
    const rows = [];
    rows.push(execGateRow("Trading halted", g.trading_halted ? "HALTED" : "Armed", !g.trading_halted, g.halt_reason || ""));
    rows.push(execGateRow("Scan loop", g.scan_paused ? "Paused" : "Running", !g.scan_paused, ""));
    rows.push(execGateRow("OR collected", g.or_collected_date || "\u2014", !!g.or_collected_date, g.or_collected_date ? "" : "pending"));
    // v5.13.4 \u2014 same Phase 2 chip set rendered on the Main view.
    const ts = (s && s.tiger_sovereign) || {};
    const phase2List = Array.isArray(ts.phase2) ? ts.phase2 : [];
    if (phase2List.length) {
      const phase1 = ts.phase1 || {};
      const longPermit = !!(phase1.long && phase1.long.permit);
      const shortPermit = !!(phase1.short && phase1.short.permit);
      const phase3List = Array.isArray(ts.phase3) ? ts.phase3 : [];
      const phase3ByTicker = {};
      for (const r of phase3List) {
        if (!r || !r.ticker) continue;
        if (!phase3ByTicker[r.ticker]) phase3ByTicker[r.ticker] = [];
        phase3ByTicker[r.ticker].push(r);
      }
      const ff = (s && s.feature_flags) || {};
      const volOff = !ff.volume_gate_enabled;
      const notes = [];
      if (volOff) notes.push("Volume Gate runtime override: OFF");
      const sub = notes.length ? ` <span class="hint">\u2014 ${esc(notes.join(" \u00b7 "))}</span>` : "";
      rows.push(`<div class="tgate-section-label">Per-ticker \u00b7 Permit \u00b7 Vol \u00b7 Boundary${sub}</div>`);
      for (const r of phase2List) {
        rows.push(execTGateRow(r, longPermit, shortPermit, phase3ByTicker[r.ticker] || []));
      }
    }
    gates.innerHTML = rows.join("");
  }

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
    renderExecProximity(panel, s);
    renderExecSovereign(panel, s);
    renderExecGates(panel, s);
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
    // v4.0.4 \u2014 mirror Main's KPI row exactly. Equity / Cash / BP come
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

    // Gate / Regime / Session \u2014 shared market state. Pull from Main's
    // last /api/state so every tab shows the same market-wide values.
    const ms = window.__tgLastState || {};
    const gates = ms.gates || {};
    const reg = ms.regime || {};
    const gateEl = execField(panel, "k-gate");
    const gateSubEl = execField(panel, "k-gate-sub");
    applyGateTriState(gateEl, gateSubEl, gates, reg);
    const rEl = execField(panel, "k-regime");
    if (rEl) {
      const breadth = reg.breadth || "UNKNOWN";
      rEl.textContent = breadth === "UNKNOWN" ? "\u2014" : breadth;
      rEl.style.color = breadth === "BULLISH" ? "var(--up)"
                      : breadth === "BEARISH" ? "var(--down)"
                      : breadth === "NEUTRAL" ? "var(--text)"
                      : "var(--text-muted)";
    }
    setField(panel, "k-regime-sub", `RSI ${reg.rsi_regime || "\u2014"}`);
    const sEl = execField(panel, "k-session");
    if (sEl) {
      const mode = reg.mode || "\u2014";
      sEl.textContent = mode;
      sEl.style.color = mode === "DEFENSIVE" ? "var(--down)"
                      : mode === "CHOP" ? "var(--warn)"
                      : mode === "CLOSED" ? "var(--text-muted)"
                      : mode === "\u2014" ? "var(--text-muted)"
                      : "var(--up)";
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
        const rows = positions.map(p => {
          const upok = (Number(p.unrealized_pnl) || 0) >= 0;
          const color = upok ? "var(--up)" : "var(--down)";
          const sideCls = p.side === "SHORT" ? "side-short" : "side-long";
          return `<tr>
            <td style="padding:6px 10px">${esc(p.symbol)}</td>
            <td style="padding:6px 10px" class="${sideCls}">${esc(p.side)}</td>
            <td class="mono" style="padding:6px 10px;text-align:right">${fmtNum(p.qty, 0)}</td>
            <td class="mono" style="padding:6px 10px;text-align:right">${fmtNum(p.avg_entry, 2)}</td>
            <td class="mono" style="padding:6px 10px;text-align:right">${fmtNum(p.current_price, 2)}</td>
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

    // Last signal card -------------------------------------------------
    const ls = data && data.last_signal;
    const sigBody = execField(panel, "last-sig-body");
    const sigChip = execField(panel, "last-sig-chip");
    if (sigChip) sigChip.textContent = disabled ? "\u2014" : (ls && ls.kind ? ls.kind : "none");
    if (sigBody) {
      if (disabled || !ls || !ls.kind) {
        sigBody.innerHTML = `<div class="empty">No signals received yet.</div>`;
      } else {
        const px = ls.price ? " @ $" + fmtNum(ls.price, 2) : "";
        const reason = ls.reason ? ` \u00b7 ${esc(ls.reason)}` : "";
        sigBody.innerHTML = `<div class="mono" style="font-size:12px;color:var(--text-muted)">
          <span style="color:var(--accent)">${esc(ls.kind)}</span>
          ${esc(ls.ticker || "")}${px}${reason}
          <span style="color:var(--text-dim)"> \u00b7 ${esc(ls.timestamp_utc || "")}</span>
        </div>`;
      }
    }

    // Diagnostics ------------------------------------------------------
    setField(panel, "d-account", account.account_number || "\u2014");
    setField(panel, "d-status", account.status || "\u2014");
    setField(panel, "d-baseurl", (data && data.alpaca_base_url) || "\u2014");
    setField(panel, "d-error", (data && data.error) || "\u2014");

    // Market-state widgets (shared) + per-executor Today's Trades -----
    renderExecMarketState(panel);
    renderExecTrades(panel, data, disabled);
  }

  // Refresh the shared KPI cells (Gate / Regime / Session) on a given
  // executor panel from Main's last /api/state so these values match
  // across every tab. v4.0.4 \u2014 extracted from renderExecutor so the
  // __tgOnState callback can refresh them without a full executor poll.
  function refreshExecSharedKpis(panel) {
    const ms = window.__tgLastState || {};
    const gates = ms.gates || {};
    const reg = ms.regime || {};
    const gateEl = execField(panel, "k-gate");
    const gateSubEl = execField(panel, "k-gate-sub");
    applyGateTriState(gateEl, gateSubEl, gates, reg);
    const rEl = execField(panel, "k-regime");
    if (rEl) {
      const breadth = reg.breadth || "UNKNOWN";
      rEl.textContent = breadth === "UNKNOWN" ? "\u2014" : breadth;
      rEl.style.color = breadth === "BULLISH" ? "var(--up)"
                      : breadth === "BEARISH" ? "var(--down)"
                      : breadth === "NEUTRAL" ? "var(--text)"
                      : "var(--text-muted)";
    }
    setField(panel, "k-regime-sub", `RSI ${reg.rsi_regime || "\u2014"}`);
    const sEl = execField(panel, "k-session");
    if (sEl) {
      const mode = reg.mode || "\u2014";
      sEl.textContent = mode;
      sEl.style.color = mode === "DEFENSIVE" ? "var(--down)"
                      : mode === "CHOP" ? "var(--warn)"
                      : mode === "CLOSED" ? "var(--text-muted)"
                      : mode === "\u2014" ? "var(--text-muted)"
                      : "var(--up)";
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

  // ─────── v5.4.1 Shadow charts (equity / heatmap / win-rate) ─────────
  // Stable per-config palette so the same config has the same hue across
  // all three chart groups (equity curve, heatmap row, win-rate sparkline).
  const SHADOW_CFG_NAMES = [
    "TICKER+QQQ", "TICKER_ONLY", "QQQ_ONLY", "GEMINI_A",
    "BUCKET_FILL_100", "REHUNT_VOL_CONFIRM", "OOMPH_ALERT",
  ];
  const SHADOW_CFG_COLORS = {
    "TICKER+QQQ":         "#7dd3fc",
    "TICKER_ONLY":        "#34d399",
    "QQQ_ONLY":           "#fbbf24",
    "GEMINI_A":           "#c084fc",
    "BUCKET_FILL_100":    "#f472b6",
    "REHUNT_VOL_CONFIRM": "#60a5fa",
    "OOMPH_ALERT":        "#fb923c",
  };
  const __shadowChartInstances = {
    equity: {},     // {cfgName: Chart}
    winrate: {},    // {cfgName: Chart}
    heatmap: null,
  };

  // v5.5.1: click-to-isolate state. When non-null, all three chart groups
  // dim every config except this one. Clicking again (or the hint's X)
  // clears it. A single state variable keeps all three groups in sync.
  let __scIsolated = null;
  let __scLastPayload = null;

  function _scIsDim(name) {
    return __scIsolated !== null && __scIsolated !== name;
  }

  function _scAlpha(hex, frac) {
    // Convert "#rrggbb" + 0..1 fraction into "rgba(r,g,b,frac)".
    const h = (hex || "").replace("#", "");
    if (h.length !== 6) return hex;
    const r = parseInt(h.slice(0, 2), 16);
    const g = parseInt(h.slice(2, 4), 16);
    const b = parseInt(h.slice(4, 6), 16);
    return "rgba(" + r + "," + g + "," + b + "," + frac.toFixed(2) + ")";
  }

  function _scFmtUsd(v) {
    const sign = v >= 0 ? "+" : "\u2212";
    return sign + "$" + Math.abs(v).toFixed(2);
  }

  function _scFmtTs(ts) {
    // "MM/DD HH:MM ET" \u2014 best-effort parse; fall back to raw string.
    try {
      const d = new Date(ts);
      if (isNaN(d.getTime())) return String(ts);
      const opts = {
        timeZone: "America/New_York", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit", hour12: false,
      };
      const parts = new Intl.DateTimeFormat("en-US", opts)
        .formatToParts(d).reduce((a, p) => (a[p.type] = p.value, a), {});
      return parts.month + "/" + parts.day + " "
        + parts.hour + ":" + parts.minute + " ET";
    } catch (e) { return String(ts); }
  }

  function _scOnConfigClick(name) {
    // Toggle: clicking the active config clears isolation; clicking a
    // different config switches isolation to it.
    __scIsolated = (__scIsolated === name) ? null : name;
    if (__scLastPayload) _scRender(__scLastPayload);
  }

  function _scClearIsolation() {
    __scIsolated = null;
    if (__scLastPayload) _scRender(__scLastPayload);
  }

  function _scUpdateHint() {
    const wrap = document.getElementById("shadow-charts-body");
    if (!wrap) return;
    let hint = document.getElementById("shadow-isolate-hint");
    if (__scIsolated === null) {
      if (hint) hint.remove();
      return;
    }
    if (!hint) {
      hint = document.createElement("div");
      hint.id = "shadow-isolate-hint";
      hint.className = "shadow-isolate-hint";
      wrap.insertBefore(hint, wrap.firstChild);
    }
    hint.innerHTML = '<span>Showing only: <b></b> \u00b7 click to clear</span>'
      + '<button type="button" class="shadow-isolate-x" '
      + 'aria-label="clear isolation">\u00d7</button>';
    hint.querySelector("b").textContent = __scIsolated;
    const btn = hint.querySelector(".shadow-isolate-x");
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      _scClearIsolation();
    });
    hint.addEventListener("click", _scClearIsolation);
  }

  function _scText(role) {
    // Read a CSS variable from :root so chart axis/text colors match the
    // existing palette without hard-coding any new color literals.
    try {
      return getComputedStyle(document.documentElement)
        .getPropertyValue(role).trim() || "#8a96a7";
    } catch (e) { return "#8a96a7"; }
  }

  function _scChartReady() {
    return typeof window !== "undefined" && typeof window.Chart === "function";
  }

  // v5.5.9 \u2014 read open_positions for each shadow config from the
  // last /api/state payload. Used by the per-config unrealized bar
  // chart fallback when equity_curve is empty but the config has open
  // positions. Returns { <cfgName>: [ {ticker, unrealized, ...}, ... ] }
  // and a per-config sum of unrealized $.
  function _scOpenPositionsByConfig() {
    const out = {};
    try {
      const s = window.__tgLastState;
      const sp = s && s.shadow_pnl;
      const cfgs = sp && sp.configs;
      if (!Array.isArray(cfgs)) return out;
      for (const cfg of cfgs) {
        if (cfg && cfg.name) {
          out[cfg.name] = Array.isArray(cfg.open_positions)
            ? cfg.open_positions.slice() : [];
        }
      }
    } catch (e) {}
    return out;
  }

  // v5.5.9 \u2014 read css color tokens for positive/negative P&L. Falls
  // back to the existing palette swatches if the variable lookup
  // fails so we never paint a chart with no fill.
  function _scPnlColors() {
    const up = _scText("--up") || "#34d399";
    const down = _scText("--down") || "#f87171";
    return { up: up, down: down };
  }

  // v5.5.9 \u2014 build a horizontal-style bar chart of per-ticker
  // unrealized P&L for one shadow config. Bars sorted desc (gains left,
  // losses right). Capped at 30 bars (top 15 winners + 15 losers when
  // count > 30) with an "and N more" footer. Color: green for positive,
  // red for negative, sourced from the shared --up / --down tokens.
  function _scBuildBarChart(row, name, openPositions) {
    const color = SHADOW_CFG_COLORS[name] || "#7dd3fc";
    // Materialize { ticker, pnl } pairs and drop entries we can't price.
    const items = (openPositions || []).map(p => ({
      ticker: String(p.ticker || ""),
      pnl: Number(p.unrealized || 0),
    })).filter(p => p.ticker && isFinite(p.pnl));
    items.sort((a, b) => b.pnl - a.pnl);
    const total = items.reduce((acc, p) => acc + p.pnl, 0);
    const nOpen = items.length;
    let display = items;
    let hidden = 0;
    if (items.length > 30) {
      const winners = items.slice(0, 15);
      const losers = items.slice(items.length - 15);
      display = winners.concat(losers);
      hidden = items.length - display.length;
    }
    const tint = total >= 0 ? "scr-bar-up" : "scr-bar-down";
    const totalTxt = '<span class="' + tint + '">' + _scFmtUsd(total) + '</span>';
    const titleHtml =
      '<div class="scr-bar-title">' + name + ' \u00b7 ' + nOpen
      + ' open \u00b7 ' + totalTxt + ' unrealized</div>';
    const moreHtml = hidden > 0
      ? '<div class="scr-bar-more">\u2026 and ' + hidden + ' more</div>'
      : '';
    row.innerHTML = '<div class="scr-name">'
      + '<span class="scr-swatch" style="background:' + color + '"></span>'
      + name + '</div>'
      + '<div class="scr-canvas-wrap scr-bar-wrap">'
      + titleHtml
      + '<canvas></canvas>'
      + moreHtml
      + '</div>';
    if (!_scChartReady()) return;
    const canvas = row.querySelector("canvas");
    const labels = display.map(p => p.ticker);
    const values = display.map(p => p.pnl);
    const palette = _scPnlColors();
    const dim = _scIsDim(name);
    const dimMul = dim ? 0.30 : 1.0;
    const colors = values.map(v => {
      const base = v >= 0 ? palette.up : palette.down;
      return _scAlpha(base, 0.85 * dimMul);
    });
    const prev = __shadowChartInstances.equity[name];
    if (prev) { try { prev.destroy(); } catch (e) {} }
    row.style.cursor = "pointer";
    row.addEventListener("click", () => _scOnConfigClick(name));
    __shadowChartInstances.equity[name] = new window.Chart(canvas, {
      type: "bar",
      data: { labels: labels, datasets: [{
        data: values,
        backgroundColor: colors,
        borderColor: colors,
        borderWidth: 0,
      }]},
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        onClick: () => _scOnConfigClick(name),
        plugins: {
          legend: { display: false },
          tooltip: {
            enabled: true,
            callbacks: {
              label: ctx => {
                const i = ctx.dataIndex;
                return labels[i] + " \u00b7 " + _scFmtUsd(values[i]);
              },
            },
          },
        },
        scales: {
          x: {
            ticks: {
              color: _scText("--text-dim"), font: { size: 9 },
              autoSkip: true, maxRotation: 0,
            },
            grid: { display: false },
          },
          y: {
            ticks: { color: _scText("--text-dim"), font: { size: 9 } },
            grid: { color: _scText("--border"), drawTicks: false },
          },
        },
      },
    });
  }

  function _scBuildEquityRows(configs) {
    const wrap = document.getElementById("shadow-equity-body");
    if (!wrap) return;
    wrap.innerHTML = "";
    // v5.5.9 \u2014 pull per-config open_positions from the last
    // /api/state snapshot so we can draw a per-ticker unrealized bar
    // chart fallback when equity_curve is empty.
    const openByCfg = _scOpenPositionsByConfig();
    let renderedAny = false;
    for (const name of SHADOW_CFG_NAMES) {
      const data = (configs[name] && configs[name].equity_curve) || [];
      const opens = openByCfg[name] || [];
      // v5.5.9 B2 \u2014 hide configs with neither closed nor open trades.
      if (!data.length && opens.length === 0) continue;
      const row = document.createElement("div");
      row.className = "shadow-chart-row";
      wrap.appendChild(row);
      renderedAny = true;
      // v5.5.9 \u2014 fallback: when there are no closed trades but open
      // positions exist, draw a per-ticker unrealized bar chart instead
      // of the "no closed trades" placeholder.
      if (!data.length) {
        _scBuildBarChart(row, name, opens);
        continue;
      }
      const color = SHADOW_CFG_COLORS[name] || "#7dd3fc";
      row.innerHTML = '<div class="scr-name">'
        + '<span class="scr-swatch" style="background:' + color + '"></span>'
        + name + '</div>'
        + '<div class="scr-canvas-wrap"><canvas></canvas></div>';
      if (!_scChartReady()) continue;
      const canvas = row.querySelector("canvas");
      const labels = data.map(p => p.ts);
      const values = data.map(p => p.cum_pnl);
      const prev = __shadowChartInstances.equity[name];
      if (prev) { try { prev.destroy(); } catch (e) {} }
      // v5.5.1: dim non-isolated configs to ~20% opacity.
      const dim = _scIsDim(name);
      const stroke = dim ? _scAlpha(color, 0.20) : color;
      const fill = dim ? _scAlpha(color, 0.05) : (color + "22");
      // v5.5.1: clicking the row name or the canvas isolates this config.
      row.style.cursor = "pointer";
      row.addEventListener("click", () => _scOnConfigClick(name));
      __shadowChartInstances.equity[name] = new window.Chart(canvas, {
        type: "line",
        data: { labels: labels, datasets: [{
          data: values, borderColor: stroke, backgroundColor: fill,
          fill: true, pointRadius: 0, borderWidth: 1.5, tension: 0.15,
        }]},
        options: {
          responsive: true, maintainAspectRatio: false, animation: false,
          onClick: () => _scOnConfigClick(name),
          plugins: {
            legend: { display: false },
            tooltip: {
              enabled: true,
              callbacks: {
                label: ctx => {
                  const i = ctx.dataIndex;
                  const ts = labels[i];
                  const v = values[i];
                  return _scFmtTs(ts) + " \u00b7 " + _scFmtUsd(v)
                    + " \u00b7 " + name;
                },
              },
            },
          },
          scales: {
            x: { display: false },
            y: { ticks: { color: _scText("--text-dim"), font: { size: 9 } },
                 grid: { color: _scText("--border"), drawTicks: false } },
          },
        },
      });
    }
    // v5.5.9 B2 \u2014 if every config was hidden (no closed AND no open
    // trades anywhere), keep a single "Waiting for shadow data" message
    // so the section never renders as a totally blank stripe.
    if (!renderedAny) {
      wrap.innerHTML = '<div class="empty">Waiting for shadow data\u2026</div>';
    }
  }

  function _scBuildWinrateRows(configs) {
    const wrap = document.getElementById("shadow-winrate-body");
    if (!wrap) return;
    wrap.innerHTML = "";
    let any = false;
    for (const name of SHADOW_CFG_NAMES) {
      const data = (configs[name] && configs[name].win_rate_rolling) || [];
      // Hide configs with < 20 closed trades (insufficient data for
      // a 20-trade rolling window). The endpoint already filters to
      // trade_idx >= 20, so an empty list means "not enough trades yet."
      if (!data.length) continue;
      any = true;
      const row = document.createElement("div");
      row.className = "shadow-chart-row scr-sparkline";
      const color = SHADOW_CFG_COLORS[name] || "#7dd3fc";
      row.innerHTML = '<div class="scr-name">'
        + '<span class="scr-swatch" style="background:' + color + '"></span>'
        + name + '</div>'
        + '<div class="scr-canvas-wrap"><canvas></canvas></div>';
      wrap.appendChild(row);
      if (!_scChartReady()) continue;
      const canvas = row.querySelector("canvas");
      const labels = data.map(p => p.trade_idx);
      const values = data.map(p => p.win_rate);
      const prev = __shadowChartInstances.winrate[name];
      if (prev) { try { prev.destroy(); } catch (e) {} }
      const dim = _scIsDim(name);
      const stroke = dim ? _scAlpha(color, 0.20) : color;
      row.style.cursor = "pointer";
      row.addEventListener("click", () => _scOnConfigClick(name));
      __shadowChartInstances.winrate[name] = new window.Chart(canvas, {
        type: "line",
        data: { labels: labels, datasets: [{
          data: values, borderColor: stroke, backgroundColor: stroke,
          fill: false, pointRadius: 0, borderWidth: 1.4, tension: 0.2,
        }]},
        options: {
          responsive: true, maintainAspectRatio: false, animation: false,
          onClick: () => _scOnConfigClick(name),
          plugins: {
            legend: { display: false },
            tooltip: {
              enabled: true,
              callbacks: {
                label: ctx => {
                  const i = ctx.dataIndex;
                  const idx = labels[i];
                  const wr = values[i];
                  const pct = (wr * 100).toFixed(1) + "%";
                  return name + " \u00b7 trade #" + idx + " \u00b7 " + pct;
                },
              },
            },
          },
          scales: {
            x: { display: false },
            y: { min: 0, max: 1, ticks: { color: _scText("--text-dim"), font: { size: 9 },
                  callback: v => Number(v).toFixed(2) },
                 grid: { color: _scText("--border"), drawTicks: false } },
          },
        },
      });
    }
    if (!any) {
      wrap.innerHTML = '<div class="scr-empty">'
        + 'no config has reached the 20-trade window yet</div>';
    }
  }

  function _scBuildHeatmap(configs) {
    const canvas = document.getElementById("shadow-heatmap-canvas");
    if (!canvas) return;
    // v5.5.9 B2 \u2014 only include configs that have closed-trade
    // daily_pnl entries. Configs with neither closed nor open trades
    // are hidden from the y-axis so the heatmap doesn't paint blank
    // rows for the 6/7 inactive configs while only OOMPH_ALERT has data.
    const cfgNames = SHADOW_CFG_NAMES.filter(name => {
      const dp = (configs[name] && configs[name].daily_pnl) || [];
      return dp.length > 0;
    });
    // Collect the union of trading days across all configs.
    const dayMap = {};
    for (const name of cfgNames) {
      const dp = (configs[name] && configs[name].daily_pnl) || [];
      for (const d of dp) {
        if (!dayMap[d.date]) dayMap[d.date] = {};
        dayMap[d.date][name] = d.pnl;
      }
    }
    const days = Object.keys(dayMap).sort();
    if (!_scChartReady()) return;
    if (__shadowChartInstances.heatmap) {
      try { __shadowChartInstances.heatmap.destroy(); } catch (e) {}
      __shadowChartInstances.heatmap = null;
    }
    if (!days.length) {
      const ctx = canvas.getContext("2d");
      const w = canvas.clientWidth || 600, h = canvas.clientHeight || 300;
      canvas.width = w; canvas.height = h;
      ctx.clearRect(0, 0, w, h);
      ctx.fillStyle = _scText("--text-dim");
      ctx.font = "11px JetBrains Mono, monospace";
      ctx.fillText("no closed trades yet", 14, 22);
      return;
    }
    // Find max abs P&L across all (config, day) cells for color scaling.
    let absMax = 0;
    for (const d of days) {
      for (const name of cfgNames) {
        const v = dayMap[d][name];
        if (typeof v === "number" && Math.abs(v) > absMax) absMax = Math.abs(v);
      }
    }
    if (absMax <= 0) absMax = 1;
    // Build a scatter chart with an emulated heatmap: one point per cell
    // sized to a square. Chart.js v4 ships only line/bar/scatter natively,
    // and bringing in the matrix plugin would add a second CDN dependency.
    // v5.5.1: also stash per-day trade counts so tooltips can show them.
    const tradeCountByDayCfg = {};
    for (const name of cfgNames) {
      const dp = (configs[name] && configs[name].daily_pnl) || [];
      for (const d of dp) {
        const k = d.date + "|" + name;
        tradeCountByDayCfg[k] = d.trades;
      }
    }
    const points = [];
    for (let yi = 0; yi < cfgNames.length; yi++) {
      const name = cfgNames[yi];
      for (let xi = 0; xi < days.length; xi++) {
        const v = (dayMap[days[xi]] || {})[name];
        if (typeof v !== "number") continue;
        const intensity = Math.min(1, Math.abs(v) / absMax);
        let alpha = 0.18 + 0.72 * intensity;
        // v5.5.1: dim non-isolated rows to ~20% of their natural opacity.
        if (_scIsDim(name)) alpha = alpha * 0.20;
        const baseColor = v >= 0 ? "52,211,153" : "248,113,113";
        points.push({
          x: xi, y: yi, _date: days[xi], _name: name, _pnl: v,
          _trades: tradeCountByDayCfg[days[xi] + "|" + name] || 0,
          backgroundColor: "rgba(" + baseColor + "," + alpha.toFixed(2) + ")",
        });
      }
    }
    __shadowChartInstances.heatmap = new window.Chart(canvas, {
      type: "scatter",
      data: { datasets: [{
        data: points,
        pointStyle: "rect",
        pointRadius: 12,
        pointHoverRadius: 14,
        backgroundColor: ctx => ctx.raw && ctx.raw.backgroundColor,
        borderColor: "rgba(0,0,0,0)",
      }]},
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        onClick: (evt, items) => {
          // v5.5.1: clicking a heatmap cell isolates that config; clicking
          // empty space clears isolation.
          if (items && items.length) {
            const r = items[0].element && items[0].element.$context
              && items[0].element.$context.raw;
            const pt = r || (points[items[0].index] || null);
            if (pt && pt._name) { _scOnConfigClick(pt._name); return; }
          }
          _scClearIsolation();
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: ctx => {
                const r = ctx.raw || {};
                const n = r._trades || 0;
                return r._name + " \u00b7 " + r._date + " \u00b7 "
                  + _scFmtUsd(r._pnl)
                  + " \u00b7 " + n + " trade" + (n === 1 ? "" : "s");
              },
            },
          },
        },
        scales: {
          x: {
            type: "linear", min: -0.5, max: days.length - 0.5,
            ticks: {
              color: _scText("--text-dim"), font: { size: 9 },
              stepSize: 1, autoSkip: true, maxRotation: 0,
              callback: v => {
                const i = Math.round(v);
                return (i >= 0 && i < days.length) ? days[i].slice(5) : "";
              },
            },
            grid: { color: _scText("--border"), drawTicks: false },
          },
          y: {
            type: "linear", min: -0.5, max: cfgNames.length - 0.5,
            reverse: true,
            ticks: {
              color: _scText("--text-dim"), font: { size: 9 },
              stepSize: 1, autoSkip: false,
              callback: v => {
                const i = Math.round(v);
                return cfgNames[i] || "";
              },
            },
            grid: { color: _scText("--border"), drawTicks: false },
          },
        },
      },
    });
  }

  function _scRender(payload) {
    const configs = (payload && payload.configs) || {};
    __scLastPayload = payload;
    // v5.5.9 \u2014 a config is "rendered" if it has either closed
    // trades (equity_curve) OR any current open positions. The
    // EQUITY CURVES group hides rows with neither, and the head
    // count reflects what is actually drawn.
    const openByCfg = _scOpenPositionsByConfig();
    let withData = 0;
    for (const n of SHADOW_CFG_NAMES) {
      const closed = configs[n] && configs[n].equity_curve
        && configs[n].equity_curve.length;
      const opens = (openByCfg[n] || []).length;
      if (closed || opens) withData += 1;
    }
    const cnt = document.getElementById("shadow-charts-count");
    if (cnt) cnt.textContent = "\u00b7 " + withData + " / " + SHADOW_CFG_NAMES.length;
    _scBuildEquityRows(configs);
    _scBuildHeatmap(configs);
    _scBuildWinrateRows(configs);
    _scUpdateHint();
  }

  let __scInflight = false;
  async function _scPoll() {
    if (__scInflight) return;
    __scInflight = true;
    try {
      const r = await fetch("/api/shadow_charts", { credentials: "same-origin" });
      if (!r.ok) throw new Error("http " + r.status);
      const data = await r.json();
      _scRender(data);
    } catch (e) {
      // Leave whatever was last rendered alone; an empty chart group on
      // first error is still informative.
    } finally {
      __scInflight = false;
    }
  }
  window.__tgPollShadowCharts = _scPoll;

  setInterval(() => {
    if (activeTab === "shadow") _scPoll();
  }, 60000);

  // Charts toggle (collapsible). Mobile defaults to collapsed so the
  // shadow tab isn't dominated by chart real estate on a phone.
  (function _scWireToggle() {
    const head = document.getElementById("shadow-charts-head");
    const body = document.getElementById("shadow-charts-body");
    const chip = document.getElementById("shadow-charts-toggle");
    if (!head || !body) return;
    function setOpen(open) {
      body.hidden = !open;
      head.setAttribute("aria-expanded", open ? "true" : "false");
      if (chip) chip.textContent = open ? "hide" : "show";
    }
    const isMobile = (typeof window !== "undefined") && (window.innerWidth <= 720);
    setOpen(!isMobile);
    head.addEventListener("click", () => setOpen(body.hidden === true));
    head.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        setOpen(body.hidden === true);
      }
    });
  })();

  // -------------------------------------------------------------------
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
            return '<option value="' + escAttr(r.position_id) + '">' + escHtml(label) + '</option>';
          }).join("");
          if (cur && rows.some(r => r.position_id === cur)) posEl.value = cur;
        }
      } catch (e) {
        if (statusEl) statusEl.textContent = "error: " + e.message;
      }
    }

    function escHtml(s) { return String(s == null ? "" : s).replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c])); }
    function escAttr(s) { return escHtml(s).replace(/"/g, "&quot;"); }

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
        const row = document.createElement("div");
        row.className = "lifecycle-row";
        row.style.cssText = "padding:8px 14px;border-bottom:1px solid var(--border);font-family:inherit";
        const reason = ev.reason_text ? '<div style="color:var(--text-dim);font-size:11px;margin-top:2px">' + escHtml(ev.reason_text) + '</div>' : "";
        row.innerHTML =
          '<div style="display:flex;gap:10px;align-items:baseline">' +
          '  <span style="font-size:10px;color:#5b6572;font-family:monospace">#' + (ev.event_seq || 0) + '</span>' +
          '  <span style="font-size:10.5px;color:var(--text-dim);font-family:monospace">' + escHtml(ev.event_ts_utc || "") + '</span>' +
          '  <span class="lifecycle-chip" style="background:' + color + '22;color:' + color + ';border:1px solid ' + color + '55;padding:1px 7px;border-radius:9px;font-size:10.5px;letter-spacing:.04em">' + escHtml(ev.event_type) + '</span>' +
          '</div>' + reason +
          '<pre class="lifecycle-payload" style="display:none;margin:6px 0 0;padding:8px;background:var(--surface-2);border-radius:4px;font-size:11px;color:var(--text-muted);max-height:300px;overflow:auto">' + escHtml(JSON.stringify(ev.payload || {}, null, 2)) + '</pre>';
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
          summaryEl.textContent = positionId + " · " + evList.length + " events";
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
