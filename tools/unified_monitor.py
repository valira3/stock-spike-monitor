"""v9.1.18 -- unified monitor.

Consolidates three pre-v9.1.18 cron workflows into one:

  state-snapshot.yml    -> dashboard endpoints (/api/state + /api/executor
                           + /api/trade_log) snapshotted to a branch
  alpaca-snapshot.yml   -> Alpaca account state per-portfolio snapshotted
                           to a branch
  dashboard-monitor.yml -> dashboard polling + invariant checks + Telegram
                           alerting

Now each runs once per 5-min cron tick instead of 3 separate workflows
on 3 separate schedules with 3 separate GH cron contention slots.

Single output: `data/monitor/latest.json` on the `monitor-live` branch.
Plus daily JSONL history at `data/monitor/<YYYY-MM-DD>.jsonl`. Plus
Telegram alert on invariant violation (same alert path as the retired
dashboard-monitor).

Shape:

    {
      "schema_version": 1,
      "captured_at_utc": "2026-05-13T..",
      "dashboard": { "endpoints": {/api/state: ..., ...} },
      "alpaca":    { "portfolios": {main: ..., val: ..., gene: ...} },
      "railway_logs": { "tail": [...], "filter_summary": {...} },
      "invariants": { "results": [...], "failed_count": N }
    }

Required env (combined surface):

    DASHBOARD_BASE_URL          dashboard pull
    DASHBOARD_PASSWORD          dashboard pull
    VAL_ALPACA_PAPER_KEY        alpaca pull (per-portfolio)
    VAL_ALPACA_PAPER_SECRET
    GENE_ALPACA_PAPER_KEY
    GENE_ALPACA_PAPER_SECRET
    [opt] MAIN_ALPACA_PAPER_KEY / _SECRET
    RAILWAY_API_TOKEN           railway logs
    RAILWAY_SERVICE_ID          railway logs
    TELEGRAM_TP_TOKEN           alerts
    TELEGRAM_TP_CHAT_ID         alerts

Optional env:

    UNIFIED_MONITOR_DIR         output dir (default: data/monitor)
    UNIFIED_MONITOR_QUIET       1 = suppress per-section progress logs
    MONITOR_DRY_RUN             1 = skip Telegram side-effects

Exit codes:

    0  full success (all data pulled + all invariants pass)
    1  config error (DASHBOARD_BASE_URL/PASSWORD missing)
    2  dashboard unreachable (snapshot incomplete; aborts before alpaca)
    3  serialization error
    4  invariants failed (data still written, Telegram sent if configured)
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("unified_monitor")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


# ----- env + log helpers ----------------------------------------------


def _require_env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        print(f"::error::{name} env var is not set", flush=True)
        sys.exit(1)
    return v


def _log(msg: str) -> None:
    if os.environ.get("UNIFIED_MONITOR_QUIET") == "1":
        return
    print(msg, flush=True)


# ----- section: dashboard ---------------------------------------------


DASHBOARD_ENDPOINTS: tuple[str, ...] = (
    "/api/state",
    "/api/executor/val",
    "/api/executor/gene",
    "/api/trade_log?limit=5000",
)


def _pull_dashboard(base: str, password: str) -> dict[str, Any]:
    """Returns {endpoint: payload_or_error_dict} for every endpoint."""
    from tools.dashboard_monitor import DashboardClient

    out: dict[str, Any] = {}
    try:
        client = DashboardClient(base, password, timeout=20.0)
        client.login()
    except Exception as e:
        return {"__login_error__": str(e)}
    for path in DASHBOARD_ENDPOINTS:
        t0 = time.time()
        try:
            out[path] = client.get_json(path)
            _log(f"  GET {path} OK ({time.time() - t0:.2f}s)")
        except Exception as e:
            out[path] = {"__error__": str(e)}
            _log(f"  GET {path} FAILED: {e}")
    return out


# ----- section: alpaca ------------------------------------------------


def _pull_alpaca_all() -> dict[str, Any]:
    """Per-portfolio Alpaca snapshot. Reuses tools.alpaca_snapshot
    helpers for shape consistency with the (now-retired)
    alpaca-snapshot.yml output -- downstream readers don't have to
    learn two schemas.
    """
    from tools.alpaca_snapshot import PORTFOLIOS, _pull_portfolio

    out: dict[str, Any] = {}
    any_ok = False
    for pid in PORTFOLIOS:
        snap = _pull_portfolio(pid)
        out[pid] = snap
        if "__error__" in snap:
            _log(f"  alpaca {pid} FAILED: {snap['__error__']}")
        else:
            n_pos = len(snap.get("positions") or [])
            n_ord = len(snap.get("orders_today") or [])
            eq = (snap.get("account") or {}).get("equity")
            _log(f"  alpaca {pid} OK  equity=${eq}  pos={n_pos}  orders={n_ord}")
            any_ok = True
    return {"portfolios": out, "_any_ok": any_ok}


# ----- section: railway logs ------------------------------------------


# Forensic tag filter. Today's strategy emits these via logger.info / .warning
# at every meaningful decision point. The monitor only needs the lines
# the operator would care about during a postmortem.
RAILWAY_FORENSIC_PATTERN = (
    r"\[V9\d{2}-|"           # v9 forensic tags ([V900-MBR-REJECT], [V910-EOD-*], [V917-TIME-CUTOFF-REJECT], ...)
    r"\[V79-ORB-|"           # v79 ORB lifecycle (BOOT/RESET/ENTRY/EXIT/...)
    r"\[V10-FIRE\]|"         # broker-fire dispatch
    r"\[V834-|"              # engine state persistence
    r"\[V8\d{2}-|"           # other v8 forensic tags
    r"ENTRY|EXIT|"           # generic entry/exit lines
    r"TRADE_CLOSED|"
    r"Traceback|"            # python errors
    r"ERROR|"                # generic error
    r"daily_kill|killswitch" # safety triggers
)
RAILWAY_LOG_LIMIT = 2000


def _pull_railway_logs() -> dict[str, Any]:
    """Tail recent Railway logs filtered to forensic patterns. Falls
    back to empty result if RAILWAY_API_TOKEN / RAILWAY_SERVICE_ID not
    configured (the monitor is still useful without log access)."""
    if not os.environ.get("RAILWAY_API_TOKEN"):
        _log("  railway logs SKIPPED -- RAILWAY_API_TOKEN unset")
        return {"__skipped__": "no token"}
    if not os.environ.get("RAILWAY_SERVICE_ID"):
        _log("  railway logs SKIPPED -- RAILWAY_SERVICE_ID unset")
        return {"__skipped__": "no service id"}
    try:
        from tools.railway_log_tail import (
            fetch_recent_logs, grep_logs, probe_railway_access,
        )
    except Exception as e:
        return {"__error__": f"railway_log_tail import: {e}"}
    probe = probe_railway_access()
    if probe.get("status") != "ok":
        _log(f"  railway logs probe FAILED: {probe.get('status')}")
        return {"__probe_fail__": probe}
    try:
        rows = fetch_recent_logs(limit=RAILWAY_LOG_LIMIT) or []
    except Exception as e:
        return {"__error__": f"fetch: {e}"}
    try:
        forensic = grep_logs(RAILWAY_FORENSIC_PATTERN, limit=RAILWAY_LOG_LIMIT) or []
    except Exception:
        forensic = []
    summary: dict[str, int] = {}
    for row in forensic:
        msg = str(row.get("message", "") or row.get("text", ""))
        for tag in ("[V900-", "[V910-", "[V917-", "[V79-ORB-", "[V10-FIRE]",
                    "Traceback", "ERROR"):
            if tag in msg:
                summary[tag] = summary.get(tag, 0) + 1
                break
    _log(f"  railway logs OK  total={len(rows)} forensic={len(forensic)}")
    return {
        "total_fetched": len(rows),
        "forensic_matches": forensic[-200:],  # cap committed size
        "filter_summary": summary,
        "probe": probe,
    }


# ----- section: invariants --------------------------------------------


def _run_invariants(dashboard_payload: dict[str, Any]) -> dict[str, Any]:
    """Run the production invariant battery against the freshly-pulled
    dashboard data. Reuses INVARIANTS + InvariantContext from the
    pre-v9.1.18 dashboard_monitor_invariants module so behavior matches
    the retired dashboard-monitor workflow exactly.
    """
    from tools.dashboard_monitor_invariants import INVARIANTS, InvariantContext

    state = dashboard_payload.get("/api/state")
    val = dashboard_payload.get("/api/executor/val")
    gene = dashboard_payload.get("/api/executor/gene")
    trade_log = dashboard_payload.get("/api/trade_log?limit=5000")
    ctx = InvariantContext(
        state=state if isinstance(state, dict) else None,
        executors={
            "val": val if isinstance(val, dict) else None,
            "gene": gene if isinstance(gene, dict) else None,
        },
        trade_log=trade_log if isinstance(trade_log, dict) else None,
    )
    results: list[dict[str, Any]] = []
    failed = 0
    for fn in INVARIANTS:
        try:
            r = fn(ctx)
        except Exception as e:
            r = {"name": getattr(fn, "__name__", str(fn)),
                 "status": "error",
                 "summary": f"raised: {type(e).__name__}: {str(e)[:200]}"}
        results.append(r)
        if r.get("status") == "fail":
            failed += 1
    _log(f"  invariants: {len(results) - failed}/{len(results)} pass, {failed} fail")
    return {"results": results, "failed_count": failed}


# ----- section: telegram alert ----------------------------------------


def _maybe_alert_telegram(invariants: dict[str, Any]) -> None:
    """Mirror dashboard-monitor's alert path: if any invariants failed,
    post a single Telegram message to TELEGRAM_TP_CHAT_ID. No-op if
    creds aren't set or MONITOR_DRY_RUN=1.
    """
    failed = [r for r in invariants.get("results", [])
              if r.get("status") == "fail"]
    if not failed:
        return
    token = (os.environ.get("TELEGRAM_TP_TOKEN") or "").strip()
    chat_id = (os.environ.get("TELEGRAM_TP_CHAT_ID") or "").strip()
    if not token or not chat_id:
        _log(f"  telegram alert SKIPPED -- no creds; would have sent {len(failed)} failures")
        return
    try:
        from tools.dashboard_monitor import send_telegram, _format_violation_telegram
    except Exception as e:
        _log(f"  telegram import failed: {e}")
        return
    msg = _format_violation_telegram(failed)
    dry = os.environ.get("MONITOR_DRY_RUN") == "1"
    ok = send_telegram(token, chat_id, msg, dry_run=dry)
    _log(f"  telegram alert sent={ok} (dry={dry})")


# ----- write + main ---------------------------------------------------


def _write_outputs(snapshot: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    latest = out_dir / "latest.json"
    latest.write_text(
        json.dumps(snapshot, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    ts = snapshot["captured_at_utc"]
    day = ts.split("T", 1)[0]
    daily = out_dir / f"{day}.jsonl"
    line = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str)
    with daily.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return latest, daily


def main() -> int:
    base = _require_env("DASHBOARD_BASE_URL")
    password = _require_env("DASHBOARD_PASSWORD")
    out_dir = Path(os.environ.get(
        "UNIFIED_MONITOR_DIR", "data/monitor"
    )).resolve()

    _log(f"[unified-monitor] base={base} out={out_dir}")

    _log("=== dashboard ===")
    dashboard_endpoints = _pull_dashboard(base, password)
    state = dashboard_endpoints.get("/api/state")
    if isinstance(state, dict) and "__error__" in state:
        print(f"::error::/api/state unreachable: {state}", flush=True)
        return 2
    if "__login_error__" in dashboard_endpoints:
        print(f"::error::login failed: {dashboard_endpoints['__login_error__']}",
              flush=True)
        return 2

    _log("=== alpaca ===")
    alpaca = _pull_alpaca_all()

    _log("=== railway logs ===")
    railway = _pull_railway_logs()

    _log("=== invariants ===")
    invariants = _run_invariants(dashboard_endpoints)

    snapshot = {
        "schema_version": 1,
        "captured_at_utc": datetime.now(timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dashboard_base_url": base,
        "dashboard": {"endpoints": dashboard_endpoints},
        "alpaca": alpaca,
        "railway_logs": railway,
        "invariants": invariants,
    }

    try:
        latest, daily = _write_outputs(snapshot, out_dir)
        _log(f"  wrote {latest}")
        _log(f"  appended {daily}")
    except Exception as e:
        print(f"::error::write failed: {e}", flush=True)
        return 3

    # Alerting after write -- a failed Telegram should NOT block the
    # data commit (the operator can still inspect monitor-live).
    _maybe_alert_telegram(invariants)

    if invariants.get("failed_count", 0) > 0:
        # Non-zero exit signals "data committed but at least one
        # invariant violated". The workflow's downstream step uses
        # this to decide if it needs to file a GitHub issue too.
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
