(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

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
          ? ` <span class="trail-badge" title="Trail stop is armed \u2014 the effective stop now follows price, not the original hard stop">TRAIL</span>`
          : "";
        // v5.13.10 — SB (Sovereign Brake distance) column removed
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
        return `<tr>
          <td><span class="ticker">${escapeHtml(p.ticker)} <span class="mark ${markCls}" title="${escapeHtml(dotTitle)}">●</span></span>${phaseBadge}</td>
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
          <th title="Symbol \u00b7 colored dot shows side (green = long, red = short)">Ticker</th>
          <th title="LONG = bought to open. SHORT = sold to open.">Side</th>
          <th class="right" title="Number of shares">Sh</th>
          <th class="right" title="Average fill price when the position opened">Entry</th>
          <th class="right" title="Latest mark price">Mark</th>
          <th class="right" title="Effective stop \u2014 trail stop if armed (TRAIL badge), otherwise the hard stop">Stop</th>
          <th class="right" title="Unrealized profit/loss in dollars at the current mark">Unreal.</th>
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

    const rowsHtml = [];
    tickers.forEach((tkr) => {
      const built = _pmtxBuildRow(tkr, idx, positionsByTicker, tradesByTicker, proximityByTicker, longPermit, shortPermit);
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
      +   '<table class="pmtx-table"><thead><tr>'
      +     '<th class="pmtx-col-titan">Titan</th>'
      +     '<th class="pmtx-col-orb" title="Permit / Boundary (Tiger Sovereign v15.0 \u00a72/\u00a73). Strike 1: two consecutive 1m closes strictly above ORH (long) or below ORL (short), with ORH/ORL frozen at exactly 09:35:59 ET. Strikes 2 & 3: two consecutive 1m closes above the running NHOD (long) or below the running NLOD (short).">ORB</th>'
      +     '<th class="pmtx-col-adx" title="Phase 3 momentum gate (v15.0 \u00a72/\u00a73). Required for entry: 5m ADX > 20 AND Alarm E = FALSE. This is a primary spec gate \u2014 if ADX \u2264 20 the bot does not open a Strike, regardless of DI\u00b1.">Trend</th>'
      +     '<th class="pmtx-col-diplus" title="Phase 3 authority check (v15.0 \u00a72/\u00a73). Required for entry: 5m DI+ > 25 (long) or 5m DI\u2212 > 25 (short). Sizing is then driven by 1m DI\u00b1: > 30 = Full Strike (100%), 25\u201330 = Scaled Strike (50% starter).">5m DI\u00b1</th>'
      +     '<th class="pmtx-col-vol" title="Volume gate (v15.0 \u00a72/\u00a73). 1m volume must be \u2265 100% of the 55-bar rolling average. REQUIRED after 10:00 AM ET; before 10:00 ET the gate auto-passes.">Vol</th>'
      +     '<th class="pmtx-col-strike" title="Strike sequence (v15.0 \u00a71). Maximum 3 Strikes per ticker per day. Sequential Requirement: a subsequent strike cannot initiate until the previous position is fully flat (Position = 0). Counters reset at 09:30:00 ET.">Strikes</th>'
      +     '<th class="pmtx-col-state" title="Per-ticker FSM \u2014 IDLE \u00b7 ARMED (Phase 1 weather + Phase 2 permit satisfied, awaiting Phase 3 authority + momentum) \u00b7 IN POS \u00b7 LOCKED (3-of-3 strikes used).">State</th>'
      +     '<th class="pmtx-col-prox" title="Live last price \u00b7 distance to the live boundary the next strike is hunting. Strike 1 hunts ORH/ORL (frozen 09:35:59); strikes 2 & 3 hunt the running NHOD/NLOD.">Dist</th>'
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
    }
    body.__pmtxApplyExpanded = _pmtxApplyExpanded;
    if (!body.__pmtxExpandWired) {
      body.addEventListener("click", function (ev) {
        const trigger = ev.target.closest("tr.pmtx-row[data-pmtx-tkr]");
        if (!trigger) return;
        const tkr = trigger.getAttribute("data-pmtx-tkr");
        const detail = body.querySelector('tr.pmtx-detail-row[data-pmtx-tkr="' + tkr + '"]');
        if (!detail) return; // no detail to expand for this row
        const set = body.__pmtxExpandedSet;
        const wasOpen = set.has(tkr);
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
          body.__pmtxExpandedSet.clear();
          if (typeof body.__pmtxApplyExpanded === "function") body.__pmtxApplyExpanded();
        }
      });
      body.__pmtxExpandWired = true;
    }
    // Re-apply after every render (innerHTML above wiped the classes).
    _pmtxApplyExpanded();
  }

  function _pmtxBuildRow(tkr, idx, positionsByTicker, tradesByTicker, proximityByTicker, longPermit, shortPermit) {
    const p2 = idx.p2[tkr] || null;
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
    const sentinelStripHtml = pos ? _pmtxSentinelStrip(p4) : "";
    const proxHasDetail = !!(prox && (
      typeof prox.price === "number"
      || prox.nearest_label
      || typeof prox.or_high === "number"
      || typeof prox.or_low === "number"
    ));
    const hasDetail = !!(pos || lastFill || proxHasDetail);
    const expandIcon = hasDetail
      ? '<span class="pmtx-expand-chev" aria-hidden="true">\u203a</span>'
      : '<span class="pmtx-expand-chev pmtx-expand-empty" aria-hidden="true"></span>';
    const rowAttrs = ' data-pmtx-tkr="' + escapeHtml(tkr) + '"' + (hasDetail ? '' : ' data-pmtx-no-detail="1"');
    const mainTr = '<tr class="pmtx-row' + rowTint + (hasDetail ? '' : ' pmtx-row-static') + '"' + rowAttrs + '>'
      + '<td class="pmtx-col-titan">' + titanHtml + '</td>'
      + '<td class="pmtx-col-orb">' + _pmtxGateCell(orb, "5-minute Opening Range break") + '</td>'
      + '<td class="pmtx-col-adx">' + _pmtxGateCell(adx, "ADX above 20 (trend strength)") + '</td>'
      + '<td class="pmtx-col-diplus">' + _pmtxGateCell(di5, _pmtxDiTooltip(p3, longPermit, shortPermit)) + '</td>'
      + '<td class="pmtx-col-vol">' + _pmtxGateCell(vol, volLabel || "Volume Bucket gate") + '</td>'
      + '<td class="pmtx-col-strike">' + strikeHtml + '</td>'
      + '<td class="pmtx-col-state">' + stateHtml + '</td>'
      + '<td class="pmtx-col-prox">' + proxHtml + '</td>'
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
        // v5.20.0 \u2014 v15.0 spec definitions per gate. Operators read
        // these to cross-check the live verdict against the verbatim
        // spec rule. Sourced from Tiger Sovereign v15.0 \u00a70\u2013\u00a74
        // + Sentinel Addendum.
        + '<div class="pmtx-spec-defs" data-pmtx-spec="v15.0">'
        +   '<div class="pmtx-spec-defs-head">Tiger Sovereign v15.0 \u00b7 spec definitions</div>'
        +   '<dl class="pmtx-spec-defs-list">'
        +     '<dt>Phase 1 \u00b7 Weather</dt>'
        +     '<dd>Long: QQQ(5m) &gt; 9-EMA <strong>AND</strong> QQQ &gt; 9:30 AM Anchor VWAP. Short: mirrored (QQQ &lt; 9-EMA <strong>AND</strong> &lt; AVWAP_0930).</dd>'
        +     '<dt>Phase 2 \u00b7 Permit (boundary)</dt>'
        +     '<dd>Two consecutive 1m closes strictly above the target level (Strike 1: ORH frozen 09:35:59 \u00b7 Strikes 2 &amp; 3: running NHOD). Short: mirrored against ORL / NLOD.</dd>'
        +     '<dt>Phase 2 \u00b7 Volume gate</dt>'
        +     '<dd>1m volume \u2265 100% of the 55-bar rolling average. <strong>REQUIRED after 10:00 AM ET</strong>; auto-passes before 10:00 ET.</dd>'
        +     '<dt>Phase 3 \u00b7 Authority</dt>'
        +     '<dd>5m DI+ &gt; 25 (long) or 5m DI\u2212 &gt; 25 (short). If FALSE \u2192 no Strike, regardless of 1m DI.</dd>'
        +     '<dt>Phase 3 \u00b7 Momentum</dt>'
        +     '<dd><strong>5m ADX &gt; 20 AND Alarm E = FALSE.</strong> Both required \u2014 ADX is now a primary spec gate.</dd>'
        +     '<dt>Phase 3 \u00b7 Sizing</dt>'
        +     '<dd>Full Strike (100%): 1m DI\u00b1 &gt; 30. Scaled Strike (50% starter): 1m DI\u00b1 in [25, 30]. Order: LIMIT at Ask\u00d71.001 (long) / Bid\u00d70.999 (short).</dd>'
        +     '<dt>Strike sequence</dt>'
        +     '<dd>Maximum 3 Strikes per ticker per day. Sequential Requirement: a subsequent strike cannot initiate until the previous position is fully flat (Position = 0).</dd>'
        +     '<dt>Alarm A \u00b7 Flash Move</dt>'
        +     '<dd>1m price move &gt; 1% against position \u2192 MARKET EXIT.</dd>'
        +     '<dt>Alarm B \u00b7 Trend Death</dt>'
        +     '<dd>5-minute candle closes across the 5m 9-EMA \u2192 MARKET EXIT.</dd>'
        +     '<dt>Alarm C \u00b7 Tiger Grip</dt>'
        +     '<dd>3 consecutive 1m ADX declines \u2192 RATCHET STOP \u00b1 0.25%.</dd>'
        +     '<dt>Alarm D \u00b7 HVP Lock</dt>'
        +     '<dd>5m ADX falls below 75% of session peak (HWM / Trade_HVP) \u2192 MARKET EXIT.</dd>'
        +     '<dt>Alarm E \u00b7 Divergence</dt>'
        +     '<dd>New extreme printed on lower (long) / higher (short) RSI(15) \u2192 RATCHET STOP \u00b1 0.25%, AND prohibits opening new Strike 2 / Strike 3.</dd>'
        +     '<dt>Risk \u00b7 Hard stop</dt>'
        +     '<dd>Resting STOP MARKET at \u2212$500 per position.</dd>'
        +     '<dt>Risk \u00b7 Daily circuit breaker</dt>'
        +     '<dd>Halt all trading and flatten if session P&amp;L reaches \u2212$1,500.</dd>'
        +     '<dt>Entry window</dt>'
        +     '<dd>09:36:00 to 15:44:59 EST. No new entries after 15:44:59.</dd>'
        +     '<dt>EOD flush</dt>'
        +     '<dd>Absolute market close at 15:49:59 EST.</dd>'
        +   '</dl>'
        + '</div>'
        + (sentinelStripHtml || "");
      tableRows += '<tr class="pmtx-detail-row" data-pmtx-tkr="' + escapeHtml(tkr) + '">'
        + '<td colspan="9">' + detailInner + '</td></tr>';
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

  // Inline 5-cell sentinel strip rendered under any open-position row.
  // A=Sovereign Brake, B=Velocity Fuse, C=Velocity Ratchet,
  // D=ADX Collapse, E=Divergence Trap. Only A/B/C are surfaced by
  // /api/state in v5.16 (sentinel.a1_pnl, sentinel.a2_velocity,
  // titan_grip.stage). D + E render as pending until the backend
  // exposes them — follow-up PR.
  function _pmtxSentinelStrip(p4) {
    const sen = (p4 && p4.sentinel) || {};
    const tg  = (p4 && p4.titan_grip) || {};
    const a1 = (typeof sen.a1_pnl === "number") ? sen.a1_pnl : null;
    const a1Th = (typeof sen.a1_threshold === "number") ? sen.a1_threshold : -500;
    const aState = (a1 === null) ? "armed" : (a1 <= a1Th ? "trip" : "safe");
    const a2 = (typeof sen.a2_velocity === "number") ? sen.a2_velocity : null;
    const a2Th = (typeof sen.a2_threshold === "number") ? sen.a2_threshold : -0.01;
    const bState = (a2 === null) ? "armed" : (a2 <= a2Th ? "trip" : "safe");
    const stage = (typeof tg.stage === "number") ? tg.stage : null;
    const cState = (stage === null) ? "armed" : (stage >= 1 ? "safe" : "armed");
    const cVal = (stage === null) ? "\u2014" : ("stage " + stage);
    const dState = "armed";
    const eState = "armed";

    function cell(letter, name, val, state) {
      return '<div class="pmtx-sentinel-cell pmtx-sen-' + state + '">'
        +   '<span class="pmtx-sen-letter">' + letter + '</span>'
        +   '<span class="pmtx-sen-name">' + escapeHtml(name) + '</span>'
        +   '<span class="pmtx-sen-val">' + escapeHtml(val) + '</span>'
        + '</div>';
    }

    return '<div class="pmtx-sentinel-strip" title="Per-position sentinel alarms (A\u2013E). Green = safe, amber = armed, red = tripped.">'
      +   cell("A", "Sov. Brake",   _pmtxMoney(a1) + " / " + _pmtxMoney(a1Th), aState)
      +   cell("B", "Velocity Fuse", _pmtxNum(a2, 4) + "/s", bState)
      +   cell("C", "Vel. Ratchet", cVal, cState)
      +   cell("D", "ADX Collapse", "\u2014", dState)
      +   cell("E", "Div. Trap",    "\u2014", eState)
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
    // v4.2.2 — extract tz token (ET/CDT/CT/PT/PST/\u2026) from
    // server_time_label tail, e.g. "Fri Apr 24 · 13:09:13 ET".
    // The client-side tick loop renders the actual HH:MM:SS every
    // second; we only cache the tz label here so the clock shows it.
    const lbl = s.server_time_label || "";
    const m = lbl.match(/\d{1,2}:\d{2}:\d{2}\s+([A-Z]{2,4})\s*$/);
    if (m) window.__tgClockTz = m[1];
    if (typeof window.__tgTickClock === "function") window.__tgTickClock();
  }

  // v5.5.7 — Main tab LAST SIGNAL card. Mirrors the exec-panel
  // formatting (kind / ticker / price / reason / timestamp). Reads
  // s.last_signal which is the paper executor's most recent emitted
  // signal (entry/exit/eod). Empty/null → "No signals received yet."
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
    renderTrades(s, sl);
    renderLastSignal(s);
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

  // v5.18.1 \u2014 expose Permit Matrix + Weather Check renderers so the
  // Val/Gene exec IIFE (separate closure below) can mount the same
  // widgets inside its panel skeletons. Both functions accept (s, panel)
  // \u2014 when panel is null they look up by id (Main DOM); when panel
  // is the exec panel root they query [data-f="..."] inside it.
  if (typeof window !== "undefined") {
    window.__tgRenderWeatherCheck = renderWeatherCheck;
    window.__tgRenderPermitMatrix = renderPermitMatrix;
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
      PHASE4_SENTINEL:  "Phase 4 sentinel — alarm A1/A2/B status changed (or fired) on an open position.",
      TITAN_GRIP_STAGE: "Titan Grip stage transition — trail engaged or ratcheted to a new stage.",
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
      alarm_codes:        "Sentinel alarm codes that fired this tick (A1=$ stop, A2=velocity, B=QQQ vs 9-EMA)",
      fired:              "True if the sentinel actually closed the position this tick",
      current_price:      "Last mark price observed at sentinel evaluation time",
      state:              "Comma-joined alarm codes (or OK)",
      stage:              "Titan Grip stage (0 = pre-arm, 1+ = trail engaged + ratcheting)",
      anchor:             "Titan Grip anchor price the trail is measured from",
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
