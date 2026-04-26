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
        // v3.4.26: show the effective stop (trail if armed, else hard
        // stop) with a small TRAIL badge so we can see at a glance
        // what is actually managing the position. Falls back to
        // p.stop when effective_stop is absent (older payloads).
        const eff = (typeof p.effective_stop === "number")
                      ? p.effective_stop : p.stop;
        const trailBadge = p.trail_active
          ? ` <span class="trail-badge" title="Trail armed — effective stop is trail_stop, not hard stop">TRAIL</span>`
          : "";
        return `<tr>
          <td><span class="ticker">${escapeHtml(p.ticker)} <span class="mark ${markCls}">●</span></span></td>
          <td><span class="${sideCls}">${p.side}</span></td>
          <td class="right">${p.shares}</td>
          <td class="right">${fmtPx(p.entry)}</td>
          <td class="right">${fmtPx(p.mark)}</td>
          <td class="right">${fmtPx(eff)}${trailBadge}</td>
          <td class="right ${pnlCls}">${fmtUsd(p.unrealized)}</td>
        </tr>`;
      }).join("");
      body.innerHTML = `<table>
        <thead><tr>
          <th>Ticker</th><th>Side</th>
          <th class="right">Sh</th><th class="right">Entry</th>
          <th class="right">Mark</th><th class="right">Stop</th>
          <th class="right">Unreal.</th>
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
      return `<div class="prox-row">
        <span class="prox-ticker">${escapeHtml(r.ticker)} ${mark}</span>
        <span class="prox-price">${fmtPx(r.price)}</span>
        <div class="prox-bar"><div class="prox-fill ${warn ? "warn" : "ok"}" style="width:${fill}%"></div></div>
        <span class="prox-pct" style="color:${warn ? 'var(--warn)' : 'var(--text-muted)'}">${pctText}</span>
      </div>`;
    }).join("");
    list.innerHTML = html;
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

  // v3.4.31 — compute the daily summary for the Today's Trades card:
  // number of opens (BUY), closes (SELL), total realized P&L across
  // closes, and win rate (wins / total closes). SELL rows carry a
  // 'pnl' field from the server; missing values are skipped silently.
  function computeTradesSummary(trades) {
    let opens = 0, closes = 0, wins = 0, realized = 0, have_pnl = 0;
    for (const t of (trades || [])) {
      const act = (t.action || "").toUpperCase();
      if (act === "BUY") opens += 1;
      else if (act === "SELL") {
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
      const isBuy  = act === "BUY";
      const isSell = act === "SELL";
      const side  = t.side || "LONG";
      const sym   = t.ticker || "—";
      const shares = t.shares;
      const px    = t.price ?? t.entry_price ?? t.exit_price;

      // Action chip — BUY (green) / SELL (red). We keep LONG/SHORT
      // colour coding on the symbol to avoid double-cueing.
      const actCls = isSell ? "act-sell" : "act-buy";
      const actLbl = act || (side === "SHORT" ? "SHORT" : "LONG");

      // v4.2.1 \u2014 tail column (between action and unit price):
      //   BUY  \u2192 total cost, subdued
      //   SELL \u2192 signed pnl + matching-colour pnl %
      let tailHtml = "\u2014";
      if (isBuy) {
        const cost = (typeof t.cost === "number" && isFinite(t.cost))
          ? t.cost
          : ((typeof shares === "number" && typeof px === "number") ? shares * px : null);
        tailHtml = cost !== null
          ? `<span class="trade-cost">${fmtUsd(cost)}</span>`
          : `<span class="trade-cost">\u2014</span>`;
      } else if (isSell) {
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

  // v3.4.29 — Sovereign Regime Shield card.
  function renderSovereign(s) {
    const body = $("srs-body");
    const chip = $("srs-status");
    if (!body || !chip) return;
    const srs = (s.regime && s.regime.sovereign) || null;
    if (!srs) {
      body.innerHTML = '<div class="empty">Regime data unavailable.</div>';
      chip.textContent = "—";
      chip.className = "chip";
      return;
    }

    // Header chip — one word status.
    const status = srs.status || "NO_PDC";
    let chipCls = "chip chip-srs-dis";
    let chipTxt = "Disarmed";
    if (status === "ARMED_LONG")      { chipCls = "chip chip-srs-armed"; chipTxt = "Long-eject armed"; }
    else if (status === "ARMED_SHORT") { chipCls = "chip chip-srs-armed"; chipTxt = "Short-eject armed"; }
    else if (status === "AWAITING")    { chipCls = "chip chip-srs-wait";  chipTxt = "Awaiting 1m close"; }
    else if (status === "NO_PDC")      { chipCls = "chip chip-srs-wait";  chipTxt = "No PDC yet"; }
    chip.textContent = chipTxt;
    chip.className = chipCls;

    // Index rows.
    const idxRow = (sym, price, pdc, delta, above) => {
      const priceTxt = (typeof price === "number") ? price.toFixed(2) : "—";
      const pdcTxt   = (typeof pdc === "number")   ? `PDC ${pdc.toFixed(2)}` : "PDC —";
      let deltaCls = "srs-delta na";
      let deltaTxt = "—";
      if (typeof delta === "number") {
        deltaCls = "srs-delta " + (delta >= 0 ? "up" : "down");
        const sign = delta >= 0 ? "+" : "";
        deltaTxt = `${sign}${delta.toFixed(2)}%`;
      }
      return `<div class="srs-idx">
        <span class="srs-sym">${escapeHtml(sym)}</span>
        <span class="srs-price">${escapeHtml(priceTxt)}</span>
        <span class="srs-pdc">${escapeHtml(pdcTxt)}</span>
        <span class="${deltaCls}">${escapeHtml(deltaTxt)}</span>
      </div>`;
    };

    // Eject tiles.
    const ejectTile = (label, fired, disabled) => {
      let cls = "srs-eject-value dis";
      let txt = "\u25CB disarmed";
      if (disabled) { cls = "srs-eject-value na"; txt = "\u25CB n/a"; }
      else if (fired) { cls = "srs-eject-value armed"; txt = "\u25CF ARMED"; }
      return `<div class="srs-eject">
        <span class="srs-eject-label">${escapeHtml(label)}</span>
        <span class="${cls}">${escapeHtml(txt)}</span>
      </div>`;
    };

    const dataMissing = (status === "NO_PDC" || status === "AWAITING");

    body.innerHTML = `<div class="srs">
      ${idxRow("SPY", srs.spy_price, srs.spy_pdc, srs.spy_delta_pct, srs.spy_above_pdc)}
      ${idxRow("QQQ", srs.qqq_price, srs.qqq_pdc, srs.qqq_delta_pct, srs.qqq_above_pdc)}
      <div class="srs-verdict">
        ${ejectTile("Long eject",  !!srs.long_eject,  dataMissing)}
        ${ejectTile("Short eject", !!srs.short_eject, dataMissing)}
      </div>
      <div class="srs-reason">${escapeHtml(srs.reason || "")}</div>
    </div>`;
  }

  function renderGates(s) {
    const g = s.gates || {};
    const gates = $("gates-body");
    const rows = [];
    rows.push(gateRow("Trading halted", g.trading_halted ? "HALTED" : "Armed", !g.trading_halted, g.halt_reason || ""));
    rows.push(gateRow("Scan loop", g.scan_paused ? "Paused" : "Running", !g.scan_paused, ""));
    rows.push(gateRow("OR collected", g.or_collected_date || "—", !!g.or_collected_date, g.or_collected_date ? "" : "pending"));

    // v3.5.x — per-ticker entry-gate chips. Volume removed (not an
    // active gate: TIGER_V2_REQUIRE_VOL defaults false). DI chip added —
    // the actual Tiger 2.0 gate after price/polarity/index.
    const per = Array.isArray(g.per_ticker) ? g.per_ticker : [];
    if (per.length) {
      const warming = per.some(r => r && r.di === null);
      const rsiUnknown = (s && s.regime && s.regime.rsi_regime === "UNKNOWN");
      const notes = [];
      if (warming) notes.push("DI warming up (needs 16 closed 5m bars)");
      if (rsiUnknown) notes.push("RSI regime: UNKNOWN (informational \u2014 not a gate)");
      const sub = notes.length ? ` <span class="hint">\u2014 ${escapeHtml(notes.join(" \u00b7 "))}</span>` : "";
      rows.push(`<div class="tgate-section-label">Per-ticker \u00b7 Brk \u00b7 PDC \u00b7 Idx \u00b7 DI${sub}</div>`);
      for (const r of per) {
        rows.push(tGateRow(r));
      }
    }
    gates.innerHTML = rows.join("");

    // v3.4.21 — next-scan countdown visible in header tick.
    const nss = (typeof g.next_scan_sec === "number") ? g.next_scan_sec : null;
    window.__nextScanSec = nss;
    updateNextScanLabel();
  }

  function tGateRow(r) {
    if (!r || !r.ticker) return "";
    const chips = [];
    if (r.side === "LONG") chips.push(`<span class="tgate-chip na">L</span>`);
    else if (r.side === "SHORT") chips.push(`<span class="tgate-chip na">S</span>`);
    else chips.push(`<span class="tgate-chip na">\u2014</span>`);
    chips.push(chipFlag("Brk", r.break));
    chips.push(chipFlag("PDC", r.polarity));
    chips.push(chipFlag("Idx", r.index));
    const diLbl = (r.di === null || typeof r.di === "undefined") ? "DI \u2026" : "DI";
    chips.push(chipFlag(diLbl, r.di, r.di === null || typeof r.di === "undefined"));
    return `<div class="tgate">
      <span class="tgate-tkr">${escapeHtml(r.ticker)}</span>
      <span class="tgate-chips">${chips.join("")}</span>
    </div>`;
  }

  function chipFlag(label, val, naOverride) {
    if (naOverride || val === null || typeof val === "undefined") {
      return `<span class="tgate-chip na">${escapeHtml(label)}</span>`;
    }
    return `<span class="tgate-chip ${val ? "on" : "off"}">${escapeHtml(label)}</span>`;
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
    renderObserver(s);
    renderSovereign(s);
    renderGates(s);
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

  function renderShadowPnL(s) {
    const sp = s && s.shadow_pnl;
    const body = $("shadow-pnl-body");
    if (!body) return;
    const cnt = $("shadow-count");
    const bestChip = $("shadow-best");
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
  const TABS = ["main", "val", "gene", "shadow"];
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
      return `<div class="prox-row">
        <span class="prox-ticker">${esc(r.ticker)} ${mark}</span>
        <span class="prox-price">${fmtPxExec(r.price)}</span>
        <div class="prox-bar"><div class="prox-fill ${warn ? "warn" : "ok"}" style="width:${fill}%"></div></div>
        <span class="prox-pct" style="color:${warn ? 'var(--warn)' : 'var(--text-muted)'}">${esc(pctText)}</span>
      </div>`;
    }).join("");
    list.innerHTML = html;
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
    const ejectTile = (label, fired, disabled) => {
      let cls = "srs-eject-value dis";
      let txt = "\u25CB disarmed";
      if (disabled) { cls = "srs-eject-value na"; txt = "\u25CB n/a"; }
      else if (fired) { cls = "srs-eject-value armed"; txt = "\u25CF ARMED"; }
      return `<div class="srs-eject">
        <span class="srs-eject-label">${esc(label)}</span>
        <span class="${cls}">${esc(txt)}</span>
      </div>`;
    };
    const dataMissing = (status === "NO_PDC" || status === "AWAITING");
    body.innerHTML = `<div class="srs">
      ${idxRow("SPY", srs.spy_price, srs.spy_pdc, srs.spy_delta_pct)}
      ${idxRow("QQQ", srs.qqq_price, srs.qqq_pdc, srs.qqq_delta_pct)}
      <div class="srs-verdict">
        ${ejectTile("Long eject",  !!srs.long_eject,  dataMissing)}
        ${ejectTile("Short eject", !!srs.short_eject, dataMissing)}
      </div>
      <div class="srs-reason">${esc(srs.reason || "")}</div>
    </div>`;
  }

  function execChipFlag(label, val, naOverride) {
    if (naOverride || val === null || typeof val === "undefined") {
      return `<span class="tgate-chip na">${esc(label)}</span>`;
    }
    return `<span class="tgate-chip ${val ? "on" : "off"}">${esc(label)}</span>`;
  }
  function execTGateRow(r) {
    if (!r || !r.ticker) return "";
    const chips = [];
    if (r.side === "LONG") chips.push(`<span class="tgate-chip na">L</span>`);
    else if (r.side === "SHORT") chips.push(`<span class="tgate-chip na">S</span>`);
    else chips.push(`<span class="tgate-chip na">\u2014</span>`);
    chips.push(execChipFlag("Brk", r.break));
    chips.push(execChipFlag("PDC", r.polarity));
    chips.push(execChipFlag("Idx", r.index));
    const diLbl = (r.di === null || typeof r.di === "undefined") ? "DI \u2026" : "DI";
    chips.push(execChipFlag(diLbl, r.di, r.di === null || typeof r.di === "undefined"));
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
    const per = Array.isArray(g.per_ticker) ? g.per_ticker : [];
    if (per.length) {
      const warming = per.some(r => r && r.di === null);
      const rsiUnknown = (s && s.regime && s.regime.rsi_regime === "UNKNOWN");
      const notes = [];
      if (warming) notes.push("DI warming up (needs 16 closed 5m bars)");
      if (rsiUnknown) notes.push("RSI regime: UNKNOWN (informational \u2014 not a gate)");
      const sub = notes.length ? ` <span class="hint">\u2014 ${esc(notes.join(" \u00b7 "))}</span>` : "";
      rows.push(`<div class="tgate-section-label">Per-ticker \u00b7 Brk \u00b7 PDC \u00b7 Idx \u00b7 DI${sub}</div>`);
      for (const r of per) rows.push(execTGateRow(r));
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
})();
