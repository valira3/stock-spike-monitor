"""tools.dashboard_monitor_invariants -- invariants for the RTH monitor.

Each invariant is a callable that takes an InvariantContext (carrying
the freshly-fetched API payloads) and returns:

    {"name": str, "ok": bool, "summary": str, "detail": str}

Where:
  - ``ok`` False triggers an alert via dashboard_monitor.py
  - ``summary`` is a one-line human-readable diagnosis
  - ``detail`` is a longer multi-line dump for the GitHub issue body

Add new invariants by appending to the INVARIANTS list at the bottom.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class InvariantContext:
    payloads: dict[str, Any]
    base_url: str


def _state(ctx: InvariantContext) -> dict | None:
    s = ctx.payloads.get("state")
    if not isinstance(s, dict) or "_fetch_error" in s:
        return None
    return s


def _exec(ctx: InvariantContext, name: str) -> dict | None:
    e = ctx.payloads.get(f"exec_{name}")
    if not isinstance(e, dict) or "_fetch_error" in e:
        return None
    return e


def _v10(ctx: InvariantContext) -> dict | None:
    s = _state(ctx)
    if not s:
        return None
    v = s.get("v10")
    if not isinstance(v, dict) or v.get("available") is False:
        return None
    if not v.get("bootstrapped"):
        return None
    return v


def _ok(name: str, summary: str = "") -> dict:
    return {"name": name, "ok": True, "summary": summary, "detail": ""}


def _fail(name: str, summary: str, detail: str = "") -> dict:
    return {"name": name, "ok": False, "summary": summary, "detail": detail}


# ---------------------------------------------------------------------------
# Fetch-level invariants
# ---------------------------------------------------------------------------


def inv_state_reachable(ctx):
    s = ctx.payloads.get("state")
    if isinstance(s, dict) and "_fetch_error" in s:
        return _fail(
            "state_reachable",
            "GET /api/state failed",
            s["_fetch_error"],
        )
    return _ok("state_reachable")


def inv_executors_reachable(ctx):
    fails = []
    for name in ("val", "gene"):
        e = ctx.payloads.get(f"exec_{name}")
        if isinstance(e, dict) and "_fetch_error" in e:
            fails.append(f"{name}: {e['_fetch_error']}")
    if fails:
        return _fail(
            "executors_reachable",
            f"executor endpoint(s) failed: {len(fails)}",
            "\n".join(fails),
        )
    return _ok("executors_reachable")


# ---------------------------------------------------------------------------
# Cross-source consistency invariants
# ---------------------------------------------------------------------------


def inv_equity_matches_baseline(ctx):
    """Headline Equity KPI must equal the v10 Backtest Baseline's Live $X.

    These two surfaces both display the operator's account balance --
    they must agree. v7.64.0 fixed the case where they disagreed
    (live_balance=0 from a stale PortfolioBook); this invariant guards
    against regression.
    """
    s = _state(ctx)
    proj = ctx.payloads.get("v10_proj") if isinstance(ctx.payloads.get("v10_proj"), dict) else None
    if not s or not proj:
        return _ok("equity_matches_baseline", "skipped: state or v10/projection missing")
    eq = (s.get("portfolio") or {}).get("equity")
    live_bal = proj.get("live_balance")
    if not isinstance(eq, (int, float)) or eq <= 0:
        return _ok("equity_matches_baseline", "skipped: state equity missing")
    if not isinstance(live_bal, (int, float)) or live_bal <= 0:
        # Backend bug from v7.0.x portfolio-book sync. Frontend overrides
        # this in v7.64.0, but the backend should ideally also return
        # the right value. Treat as a warning.
        return _fail(
            "equity_matches_baseline",
            f"/api/v10/projection.live_balance={live_bal!r} but /api/state.portfolio.equity=${eq:.2f}",
            (
                "Backend PortfolioBook.current_equity() is returning 0 -- "
                "v7.0.x portfolio-book registry sync bug. Frontend "
                "overrides this in v7.64.0 so the dashboard displays "
                "correctly, but anyone querying /api/v10/projection "
                "directly sees the bad value. Repair the registry sync "
                "in engine/portfolio_book.py."
            ),
        )
    # When both are present, allow a small tolerance for mid-tick races.
    if abs(eq - live_bal) > max(1.0, eq * 0.001):
        return _fail(
            "equity_matches_baseline",
            f"equity ${eq:.2f} != live_balance ${live_bal:.2f} (delta ${eq - live_bal:.2f})",
            "Two surfaces of the same number have drifted apart. Check the v10 projection backend path.",
        )
    return _ok("equity_matches_baseline")


def inv_val_gene_trades_match_main(ctx):
    """When ORB_PORTFOLIO_FIRE=0 (mirror mode), Val and Gene should
    have the same broker trade count as Main today.
    """
    s = _state(ctx)
    val = _exec(ctx, "val")
    gene = _exec(ctx, "gene")
    if not s or not val or not gene:
        return _ok("val_gene_trades_match_main", "skipped: state/exec missing")
    main_count = len(s.get("trades_today") or [])
    val_count = len(val.get("trades_today") or [])
    gene_count = len(gene.get("trades_today") or [])
    mismatches = []
    if val.get("enabled") is not False and val_count != main_count:
        mismatches.append(f"val={val_count} vs main={main_count}")
    if gene.get("enabled") is not False and gene_count != main_count:
        mismatches.append(f"gene={gene_count} vs main={main_count}")
    if mismatches:
        return _fail(
            "val_gene_trades_match_main",
            f"trade-count mismatch: {', '.join(mismatches)}",
            "In mirror mode (ORB_PORTFOLIO_FIRE=0) Val and Gene fire on the "
            "same signals as Main via the legacy bus, so their broker trade "
            "counts should match. A mismatch may indicate Alpaca-side "
            "rejection or a mirror-bus drift.",
        )
    return _ok("val_gene_trades_match_main")


def inv_top_ticker_within_cap(ctx):
    """No (pid, ticker) day_state may have trades_today > max_trades_per_day."""
    v10 = _v10(ctx)
    if not v10:
        return _ok("top_ticker_within_cap", "skipped: v10 not bootstrapped")
    cap = (v10.get("config") or {}).get("max_trades_per_day") or 5
    over = []
    for ds in v10.get("day_states") or []:
        n = ds.get("trades_today") or 0
        if n > cap:
            over.append(f"{ds.get('portfolio_id')}.{ds.get('ticker')}: {n} > {cap}")
    if over:
        return _fail(
            "top_ticker_within_cap",
            f"{len(over)} ticker(s) exceeded cap",
            "\n".join(over),
        )
    return _ok("top_ticker_within_cap")


def inv_open_risk_within_cap(ctx):
    """For every pid, open_risk <= max_risk_dollars + tiny epsilon."""
    v10 = _v10(ctx)
    if not v10:
        return _ok("open_risk_within_cap", "skipped: v10 not bootstrapped")
    over = []
    for pid, b in (v10.get("risk_books") or {}).items():
        used = b.get("open_risk") or 0.0
        cap = b.get("max_risk_dollars") or 0.0
        if cap > 0 and used > cap + 0.01:
            over.append(f"{pid}: open_risk=${used:.2f} > cap=${cap:.2f}")
    if over:
        return _fail(
            "open_risk_within_cap",
            f"{len(over)} pid(s) over concurrent-risk cap",
            "\n".join(over),
        )
    return _ok("open_risk_within_cap")


def inv_or_window_well_formed(ctx):
    """For every locked OR window, or_low <= or_high and or_width_pct in
    a sane range [0, 0.2]. Sentinel against off-by-one / DST glitches.
    """
    v10 = _v10(ctx)
    if not v10:
        return _ok("or_window_well_formed", "skipped: v10 not bootstrapped")
    bad = []
    for tkr, w in (v10.get("or_windows") or {}).items():
        if not w.get("locked"):
            continue
        oh, ol = w.get("or_high"), w.get("or_low")
        if not isinstance(oh, (int, float)) or not isinstance(ol, (int, float)):
            bad.append(f"{tkr}: locked but or_high/or_low not numeric ({oh!r}, {ol!r})")
            continue
        if ol > oh:
            bad.append(f"{tkr}: or_low ${ol:.2f} > or_high ${oh:.2f}")
        width = w.get("or_width_pct")
        if isinstance(width, (int, float)) and (width < 0 or width > 0.20):
            bad.append(f"{tkr}: or_width_pct {width:.4f} out of sane range")
    if bad:
        return _fail(
            "or_window_well_formed",
            f"{len(bad)} locked OR window(s) malformed",
            "\n".join(bad),
        )
    return _ok("or_window_well_formed")


def inv_v10_live_mode_on_during_rth(ctx):
    """During RTH, v10 should be bootstrapped + live_mode=true. If
    either is off the bot is in legacy fallback and the operator
    needs to know.
    """
    v10 = _state(ctx) and _state(ctx).get("v10") or {}
    if not v10:
        return _ok("v10_live_mode_on", "skipped: state missing")
    # Only flag when we're inside RTH (the monitor only runs in RTH
    # via the cron schedule, but the bot might be in PRE/POST_CLOSE
    # at the edge of the window).
    regime = (_state(ctx) or {}).get("regime") or {}
    mode = (regime.get("mode") or "").upper()
    if mode in ("PRE", "POST_CLOSE", "AFTERHOURS", ""):
        return _ok("v10_live_mode_on", "skipped: not in RTH session")
    if v10.get("available") is False:
        return _fail("v10_live_mode_on", "v10.available=false during RTH")
    if not v10.get("bootstrapped"):
        return _fail("v10_live_mode_on", "v10.bootstrapped=false during RTH")
    if not v10.get("live_mode"):
        return _fail(
            "v10_live_mode_on",
            "v10.live_mode=false during RTH -- bot in legacy fallback",
            "Check ORB_LIVE_MODE env var on Railway.",
        )
    return _ok("v10_live_mode_on")


def inv_no_phantom_positions(ctx):
    """Every position reported in /api/state.positions should also
    appear in at least one risk_book.open_count or vice-versa.

    Catches the case where a position exists at the broker but the
    engine forgot to register it with the RiskBook (would let it
    over-admit risk).
    """
    s = _state(ctx)
    v10 = _v10(ctx)
    if not s or not v10:
        return _ok("no_phantom_positions", "skipped")
    main_pos = len(s.get("positions") or [])
    main_rb = (v10.get("risk_books") or {}).get("main") or {}
    rb_open = main_rb.get("open_count") or 0
    # In mirror mode val/gene positions don't go through main's RB,
    # so we only check main here.
    if abs(main_pos - rb_open) > 0:
        return _fail(
            "no_phantom_positions",
            f"main has {main_pos} positions in /api/state but RiskBook reports open_count={rb_open}",
            "Position-tracking drift between the live book and the v10 RiskBook.",
        )
    return _ok("no_phantom_positions")


def inv_daily_kill_consistency(ctx):
    """daily_kill_triggered should be true if and only if realized_pnl_today
    has fallen at or below -daily_kill_threshold.
    """
    v10 = _v10(ctx)
    if not v10:
        return _ok("daily_kill_consistency", "skipped: v10 not bootstrapped")
    bad = []
    for pid, b in (v10.get("risk_books") or {}).items():
        thr = b.get("daily_kill_threshold") or 0
        realized = b.get("realized_pnl_today") or 0
        triggered = bool(b.get("daily_kill_triggered"))
        if thr <= 0:
            continue
        should = realized <= -thr + 0.01
        if should != triggered:
            bad.append(
                f"{pid}: triggered={triggered} but realized=${realized:.2f} vs threshold=-${thr:.2f}"
            )
    if bad:
        return _fail(
            "daily_kill_consistency",
            f"daily-kill flag inconsistent with realized P&L: {len(bad)} pid(s)",
            "\n".join(bad),
        )
    return _ok("daily_kill_consistency")


def inv_version_advertised(ctx):
    """Live bot's BOT_VERSION should be parseable and >= 7.0.0."""
    s = _state(ctx)
    if not s:
        return _ok("version_advertised", "skipped: state missing")
    # v7.72.0 -- field is `version` on the /api/state response
    # (dashboard_server.py:1945), not `bot_version`. Pre-v7.72.0 monitor
    # always tripped this invariant with `BOT_VERSION malformed: ''`.
    bv = s.get("version") or s.get("bot_version") or ""
    parts = bv.split(".")
    if len(parts) < 3:
        return _fail("version_advertised", f"BOT_VERSION malformed: {bv!r}")
    try:
        major = int(parts[0])
    except ValueError:
        return _fail("version_advertised", f"BOT_VERSION major not int: {bv!r}")
    if major < 7:
        return _fail("version_advertised", f"BOT_VERSION ({bv}) is pre-v7 -- legacy build")
    return _ok("version_advertised")


# ---------------------------------------------------------------------------
# v7.75.0 cross-check invariants (self-derived expectations vs payload)
# ---------------------------------------------------------------------------


def inv_or_locked_after_or_end(ctx: InvariantContext) -> dict:
    """v7.75.0 -- during OPEN/POWER regime, expect at least one OR
    window to be locked.

    The 2026-05-11 production incident: at 09:14 CT (10:14 ET, well
    past 09:59 ET OR-end) the dashboard showed 0/10 LOCKED while
    prices were flowing. Pre-existing invariants caught the symptom
    (`or_window_well_formed` only checks locked windows; `no phantom
    positions` had nothing to compare against). This invariant
    catches the EXISTENCE problem directly.
    """
    v10 = _v10(ctx)
    if not v10:
        return _ok("or_locked_after_or_end", "skipped: v10 not bootstrapped")
    regime = (_state(ctx) or {}).get("regime") or {}
    mode = (regime.get("mode") or "").upper()
    # OR phase is OK to be unlocked; only after the 09:35 ET ish boundary
    # do we expect locks. dashboard_server.py labels post-OR-end as OPEN.
    if mode not in ("OPEN", "POWER"):
        return _ok("or_locked_after_or_end", f"skipped: regime mode={mode!r}")
    or_windows = v10.get("or_windows") or {}
    if not or_windows:
        return _fail(
            "or_locked_after_or_end",
            f"v10.or_windows is empty during {mode} (no tickers tracked)",
            "Engine bootstrap looks shallow. Check [V79-ORB-RESET] for "
            "session-start completion and [V79-ORB-BACKFILL] (v7.74.0+) "
            "for the post-restart historical replay.",
        )
    total = len(or_windows)
    locked = sum(1 for w in or_windows.values() if w.get("locked"))
    if locked == 0:
        details = []
        for t, w in list(or_windows.items())[:10]:
            details.append(
                f"  {t}: bars_seen={w.get('bars_seen')} locked={w.get('locked')}"
            )
        return _fail(
            "or_locked_after_or_end",
            f"0/{total} OR windows locked during {mode} "
            f"(expected at least 1)",
            "Engine never closed the OR window. Likely causes: "
            "(1) bot restarted post-OR and the v7.74.0 backfill failed "
            "or skipped; (2) bar source returned None for every ticker; "
            "(3) bucket-math drift. First 10 windows:\n"
            + "\n".join(details),
        )
    return _ok(
        "or_locked_after_or_end",
        f"{locked}/{total} OR windows locked",
    )


def inv_or_window_data_quality(ctx: InvariantContext) -> dict:
    """v7.75.0 -- locked OR windows should have >= or_minutes // 2 bars.

    A locked window with only 5/30 bars indicates a sparse data feed.
    The engine already routes thin OR to BLOCKED_OR_INSUFFICIENT in
    `_lock_and_arm`, but the monitor should still flag it because
    multiple thin windows on the same day suggests an upstream
    Alpaca/Yahoo problem, not a per-ticker quirk.
    """
    v10 = _v10(ctx)
    if not v10:
        return _ok("or_window_data_quality", "skipped: v10 not bootstrapped")
    or_windows = v10.get("or_windows") or {}
    cfg = v10.get("config") or {}
    or_minutes = int(cfg.get("or_minutes") or 30)
    min_bars = or_minutes // 2
    thin = []
    for ticker, w in or_windows.items():
        if not w.get("locked"):
            continue
        bs = w.get("bars_seen") or 0
        if bs < min_bars:
            thin.append((ticker, bs))
    if len(thin) >= 3:
        rows = "\n".join(f"  {t}: bars_seen={b} (need {min_bars})"
                         for t, b in thin)
        return _fail(
            "or_window_data_quality",
            f"{len(thin)} locked OR windows with bars_seen < {min_bars}",
            "Multiple thin OR windows on the same day suggests an "
            "upstream bar-source problem (Alpaca IEX feed flapping, "
            "Yahoo intraday gaps). Per-ticker individual thin windows "
            "are routine; >=3 together is a system signal.\n" + rows,
        )
    return _ok("or_window_data_quality")


def inv_position_count_three_way(ctx: InvariantContext) -> dict:
    """v7.75.0 -- positions[] vs risk_books.main.open_count vs broker_open_n.

    Three independent surfaces for "how many positions are open":
      A. /api/state.positions length        (paper book)
      B. risk_books.main.open_count         (per-portfolio RiskBook)
      C. /api/state.portfolio.broker_open_n (Alpaca-side count)

    A and B should match exactly (the v7.62-era no_phantom_positions
    invariant covers that). A vs C can legitimately differ: Main is
    paper-only and doesn't fire to the broker, so C can be > 0 from
    Val/Gene executor positions while A == 0. But if A == 0 AND
    /api/state.portfolios.val.open_count == 0 AND
    /api/state.portfolios.gene.open_count == 0 AND C > 0, the broker
    has positions nobody internally tracks -- that's a real phantom.
    """
    s = _state(ctx)
    if not s:
        return _ok("position_count_three_way", "skipped: state missing")
    portfolios = s.get("portfolios") or {}
    main = portfolios.get("main") or {}
    val = portfolios.get("val") or {}
    gene = portfolios.get("gene") or {}
    portfolio = s.get("portfolio") or {}
    broker_open_n = int(portfolio.get("broker_open_n") or 0)
    main_count = len(main.get("positions") or s.get("positions") or [])
    val_count = len(val.get("positions") or [])
    gene_count = len(gene.get("positions") or [])
    internal_total = main_count + val_count + gene_count
    if broker_open_n > 0 and internal_total == 0:
        return _fail(
            "position_count_three_way",
            f"broker has {broker_open_n} open position(s) but all "
            "three internal books are empty -- phantom at broker",
            "Likely cause: bot was down when a broker-side fill or "
            "exit landed, or the post-restart state-restore missed "
            "Val/Gene executor positions. Manual reconciliation may "
            "be needed via /reconcile or by inspecting Alpaca's "
            "positions endpoint directly. Counts: "
            f"main={main_count} val={val_count} gene={gene_count} "
            f"broker_open_n={broker_open_n}",
        )
    return _ok(
        "position_count_three_way",
        f"main={main_count} val={val_count} gene={gene_count} "
        f"broker_open_n={broker_open_n}",
    )


def inv_equity_self_consistent(ctx: InvariantContext) -> dict:
    """v7.75.0 -- portfolio.equity must equal cash + long_mv - short_liab.

    The dashboard surfaces equity as a single computed field, but the
    components are also exposed independently. The two views of the
    same number must agree to within float precision. A divergence
    points to either a stale cash-snapshot, an unbooked fill, or a
    type/coercion bug in one of the surfaces.
    """
    s = _state(ctx)
    if not s:
        return _ok("equity_self_consistent", "skipped: state missing")
    p = s.get("portfolio") or {}
    eq = p.get("equity")
    cash = p.get("cash")
    long_mv = p.get("long_mv")
    short_liab = p.get("short_liab")
    if not all(isinstance(v, (int, float))
               for v in (eq, cash, long_mv, short_liab)):
        return _ok("equity_self_consistent", "skipped: components missing")
    derived = float(cash) + float(long_mv) - float(short_liab)
    diff = abs(float(eq) - derived)
    # Allow $1 or 0.01% slack for floating-point noise.
    tol = max(1.0, float(eq) * 1e-4)
    if diff > tol:
        return _fail(
            "equity_self_consistent",
            f"portfolio.equity ${eq:.2f} != cash + long_mv - short_liab "
            f"(derived ${derived:.2f}, delta ${diff:.2f}, tol ${tol:.2f})",
            f"Components: cash={cash} long_mv={long_mv} "
            f"short_liab={short_liab}. The dashboard's equity KPI and "
            "its position-detail breakdown have drifted. Likely a "
            "stale snapshot in one of the surfaces or an unbooked "
            "fill that updated cash but not the positions list (or "
            "vice versa). Check the _state_snapshot construction in "
            "dashboard_server.py.",
        )
    return _ok("equity_self_consistent",
               f"eq=${eq:.2f} ≈ derived ${derived:.2f}")


def inv_v10_in_pos_has_internal_position(ctx: InvariantContext) -> dict:
    """v7.76.0 -- every (portfolio, ticker) in v10 phase=IN_POS must
    have a matching entry in that portfolio's positions list.

    The 2026-05-11 production scenario the operator hit: the
    dashboard's v10 Ticker Matrix shows AAPL "IN POS" and Concurrent
    Risk reads $739/$2000, but `OPEN POSITIONS: 0` and the positions
    list is empty.

    The most likely root cause: v10 ORB admitted the entry, the FSM
    transitioned WARMUP/ARMED -> IN_POS, and the RiskBook reserved
    capacity -- but `callbacks.execute_entry` (which calls
    `broker/orders.execute_breakout`, which mutates `tg.positions`)
    failed or never ran. The FSM is now stuck IN_POS with no
    underlying position to manage. (Alternate: FSM stayed stuck
    IN_POS after the position exited normally -- bug in the exit
    path's FSM transition.)

    Either way, the bot is in an inconsistent state and the operator
    needs to know. Auto-recovery is intentionally NOT attempted here
    -- this invariant only DETECTS so the operator can choose the
    safe action (manual /reconcile, FSM reset, or close-and-reset).
    """
    s = _state(ctx)
    v10 = _v10(ctx)
    if not s or not v10:
        return _ok("v10_in_pos_has_internal_position",
                   "skipped: state or v10 missing")
    day_states = v10.get("day_states") or []
    if not day_states:
        return _ok("v10_in_pos_has_internal_position",
                   "skipped: no v10 day_states yet")

    def _ticker_set_for(pid: str) -> set[str]:
        # Try per-portfolio first, fall back to top-level positions
        # for Main (legacy / pre-v7.0.0 schema).
        portfolios = s.get("portfolios") or {}
        pbk = portfolios.get(pid) or {}
        pos = pbk.get("positions")
        if pos is None and pid == "main":
            pos = s.get("positions") or []
        pos = pos or []
        out: set[str] = set()
        for p in pos:
            if isinstance(p, dict):
                t = p.get("ticker") or p.get("symbol")
                if t:
                    out.add(str(t).upper())
        return out

    # Cache per-portfolio ticker sets (cheap, but stable per call).
    per_pid_tickers: dict[str, set[str]] = {}
    phantom_in_pos: list[dict] = []
    for ds in day_states:
        if not isinstance(ds, dict):
            continue
        phase = (ds.get("phase") or "").lower()
        if phase != "in_pos" and not ds.get("in_position"):
            continue
        pid = (ds.get("portfolio_id") or "").lower() or "main"
        ticker = (ds.get("ticker") or "").upper()
        if not ticker:
            continue
        if pid not in per_pid_tickers:
            per_pid_tickers[pid] = _ticker_set_for(pid)
        if ticker not in per_pid_tickers[pid]:
            phantom_in_pos.append({
                "pid": pid, "ticker": ticker,
                "phase": ds.get("phase"),
                "in_position": ds.get("in_position"),
                "last_entry_iso": ds.get("last_entry_iso"),
            })

    if phantom_in_pos:
        lines = []
        for ph in phantom_in_pos[:10]:
            lines.append(
                f"  {ph['pid']}/{ph['ticker']}: phase={ph['phase']!r} "
                f"in_position={ph['in_position']} "
                f"last_entry={ph['last_entry_iso']}"
            )
        return _fail(
            "v10_in_pos_has_internal_position",
            f"{len(phantom_in_pos)} phantom IN_POS state(s) -- "
            "FSM thinks open but no matching position in book",
            "v10 FSM has phase=IN_POS for one or more (portfolio, "
            "ticker) pairs that have no corresponding entry in the "
            "portfolio's positions list. Likely cause: the entry "
            "admit() succeeded and the RiskBook reserved capacity, "
            "but the actual paper-book fill (callbacks.execute_entry "
            "-> broker.orders.execute_breakout -> tg.positions[]) "
            "failed or didn't run. The bot is in an inconsistent "
            "state with reserved risk but no managed position. "
            "Safe recovery: inspect trade_log.jsonl for the entry "
            "intent, then manually reset the FSM via /reconcile or "
            "force-close the phantom slot.\n" + "\n".join(lines),
        )
    return _ok("v10_in_pos_has_internal_position")


def inv_risk_book_notional_cap_nonzero(ctx: InvariantContext) -> dict:
    """v7.76.0 -- every active RiskBook must have a nonzero notional cap.

    The 2026-05-11 Val tab production scenario: every entry rejected
    with ``risk_reject:notional_cap (would-be $293 > $0)``. Root
    cause: ``RiskBook.equity`` was seeded from
    ``PortfolioBook.current_equity()`` which returns 0 for Val/Gene
    (their `paper_cash` defaults to 0 and was never bridged from
    Alpaca's actual account equity). So `max_notional = equity *
    max_concurrent_notional_mult = 0`, blocking every entry.

    During RTH, when ORB live mode is on, every portfolio's RiskBook
    must have a nonzero ``max_notional`` (and `equity`). This catches
    both the Val/Gene-equity-seeding bug and any future regression
    that fails to populate per-portfolio equity at session start.
    """
    v10 = _v10(ctx)
    if not v10:
        return _ok("risk_book_notional_cap_nonzero",
                   "skipped: v10 not bootstrapped")
    regime = (_state(ctx) or {}).get("regime") or {}
    mode = (regime.get("mode") or "").upper()
    if mode not in ("OPEN", "POWER", "OR"):
        return _ok("risk_book_notional_cap_nonzero",
                   f"skipped: regime mode={mode!r}")
    risk_books = v10.get("risk_books") or {}
    if not risk_books:
        return _ok("risk_book_notional_cap_nonzero",
                   "skipped: no risk_books in snapshot")
    zeros = []
    for pid, rb in risk_books.items():
        if not isinstance(rb, dict):
            continue
        max_notional = rb.get("max_notional")
        equity = rb.get("equity")
        if (isinstance(max_notional, (int, float)) and max_notional <= 0) \
                or (isinstance(equity, (int, float)) and equity <= 0):
            zeros.append((pid, equity, max_notional,
                          rb.get("last_reject_reason")))
    if zeros:
        lines = []
        for pid, eq, mn, reason in zeros:
            lines.append(
                f"  {pid}: equity={eq} max_notional={mn} "
                f"last_reject={reason!r}"
            )
        return _fail(
            "risk_book_notional_cap_nonzero",
            f"{len(zeros)} RiskBook(s) have zero equity/max_notional "
            "during RTH -- entries will be rejected on notional_cap",
            "RiskBook.equity is 0 for one or more portfolios. For "
            "Val/Gene this happens when the paper_cash defaults to 0 "
            "and isn't bridged from Alpaca's account equity (the "
            "v7.76.0 engine.portfolio_equity.resolve_equity helper "
            "wires this on session start; older deployments may "
            "still be missing it). For Main it points at a stale "
            "tg.paper_cash sync.\n" + "\n".join(lines),
        )
    return _ok(
        "risk_book_notional_cap_nonzero",
        f"{len(risk_books)} books all have nonzero caps",
    )


def inv_railway_logs_clean(ctx: InvariantContext) -> dict:
    """v7.79.0 -- fetch recent Railway deployment logs and alert on
    known failure signatures.

    Many production issues never surface in /api/state because they
    happen in non-state-mutating code paths (broker rejects, sentinel
    errors, ingest disconnects). This invariant goes upstream and
    reads the bot's actual stdout/stderr via Railway's GraphQL API.

    Requires RAILWAY_API_TOKEN + RAILWAY_SERVICE_ID env vars. Falls
    back to ok-skip if either is missing or the API is unreachable.

    Severity tiers:
      - Critical signals (alpaca_error, sentinel_critical,
        uncaught_traceback): fail on any occurrence.
      - Soft signals (insufficient_cash, risk_reject_*, v15_wait_abort):
        fail only when count >= 5 in the fetched window (suggests
        systemic issue, not a one-off rejection).
      - Informational signals (ingest_disconnect): fail when count >= 3.
    """
    try:
        from tools.railway_log_tail import fetch_recent_logs, scan_for_failures
    except Exception as e:
        return _ok("railway_logs_clean", f"skipped: import failed: {e}")
    logs = fetch_recent_logs(limit=500)
    if not logs:
        return _ok(
            "railway_logs_clean",
            "skipped: Railway log fetch unavailable "
            "(missing RAILWAY_API_TOKEN/RAILWAY_SERVICE_ID or API error)",
        )
    findings = scan_for_failures(logs)
    critical_signals = ("alpaca_error", "sentinel_critical",
                        "uncaught_traceback")
    soft_signals = ("insufficient_cash", "risk_reject_notional_cap",
                    "risk_reject_other", "v15_wait_abort")
    info_signals = ("ingest_disconnect",)
    triggered: list[tuple[str, dict, str]] = []
    for name, info in findings.items():
        count = info.get("count", 0)
        if name in critical_signals and count >= 1:
            triggered.append((name, info, "CRITICAL"))
        elif name in soft_signals and count >= 5:
            triggered.append((name, info, "SOFT"))
        elif name in info_signals and count >= 3:
            triggered.append((name, info, "INFO"))
    if triggered:
        lines = []
        for name, info, tier in triggered:
            lines.append(
                f"  [{tier}] {name}: count={info['count']} "
                f"last={info['last_timestamp']} "
                f"sample={info['first_message'][:200]!r}"
            )
        return _fail(
            "railway_logs_clean",
            f"{len(triggered)} log-signature alert(s) in last {len(logs)} lines",
            "Recent Railway deployment logs contain failure signatures "
            "that indicate runtime issues not visible in /api/state. "
            "Tiers: CRITICAL=any 1 hit fails; SOFT=>=5 hits; "
            "INFO=>=3 hits. Investigate the sample message(s) below "
            "and grep Railway for the full context.\n" + "\n".join(lines),
        )
    return _ok(
        "railway_logs_clean",
        f"scanned {len(logs)} lines, "
        f"{len(findings)} sub-threshold signals",
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


INVARIANTS = [
    inv_state_reachable,
    inv_executors_reachable,
    inv_version_advertised,
    inv_v10_live_mode_on_during_rth,
    inv_equity_matches_baseline,
    inv_val_gene_trades_match_main,
    inv_top_ticker_within_cap,
    inv_open_risk_within_cap,
    inv_or_window_well_formed,
    inv_no_phantom_positions,
    inv_daily_kill_consistency,
    # v7.75.0 cross-check invariants
    inv_or_locked_after_or_end,
    inv_or_window_data_quality,
    inv_position_count_three_way,
    inv_equity_self_consistent,
    # v7.76.0 -- FSM-vs-book + RiskBook equity consistency
    inv_v10_in_pos_has_internal_position,
    inv_risk_book_notional_cap_nonzero,
    # v7.79.0 -- Railway log-tail analysis
    inv_railway_logs_clean,
]
